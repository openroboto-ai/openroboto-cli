"""Gamma uses exact event proof and never retries an uncertain charge."""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from openroboto_protocol.gamma_payment import gamma_payment_reference
from openroboto_protocol.schemas import Competition

from openroboto.commands import doctor, gamma_payment
from openroboto.competition import (
    GAMMA,
    Snapshot,
    Verdict,
    confirm_payment,
    fee_of,
    judge,
)
from openroboto.config import ConfigError, Settings
from openroboto.payment.gamma import (
    GammaPaymentError,
    check_gamma_deposit,
    execute_gamma_spend,
    recover_gamma_spend,
)

HOTKEY = "5" + "a" * 47
COLDKEY = "5" + "b" * 47
TX = "0x" + "ab" * 32
GENESIS = "0x" + "12" * 32
COMMIT = "a" * 40
FEE = {"kind": "gamma", "amount_gamma": "25"}


@pytest.fixture(autouse=True)
def isolated_shared_payment_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(gamma_payment, "SHARED_PAYMENT_ROOT", tmp_path / "shared-cache")


def state():
    return {
        "competition_id": 75,
        "hotkey_ss58": HOTKEY,
        "hf_repo_id": "author/task",
        "hf_commit": COMMIT,
    }


def reference():
    return gamma_payment_reference(
        netuid=80,
        competition_id=75,
        hotkey=HOTKEY,
        hf_repo_id="author/task",
        hf_commit=COMMIT,
    )


def spent(**changes):
    attrs = {
        "netuid": 80,
        "hotkey": HOTKEY,
        "coldkey": COLDKEY,
        "spender": COLDKEY,
        "amount": 25_000_000_000,
        "reference": reference(),
    }
    attrs.update(changes)
    return {
        "event": {
            "module_id": "SubtensorModule",
            "event_id": "GammaSpent",
            "attributes": attrs,
        }
    }


def chain(events=None):
    value = {
        "coldkey": COLDKEY,
        "spender": COLDKEY,
        "balance": 50_000_000_000,
        "daily_spend_cap": None,
    }
    substrate = Mock()
    substrate.query.return_value.value = value
    substrate.get_block_hash.return_value = GENESIS
    substrate.get_block_number.return_value = 401
    substrate.create_signed_extrinsic.return_value.extrinsic_hash = bytes.fromhex(
        TX[2:]
    )
    receipt = SimpleNamespace(
        is_success=True,
        error_message=None,
        block_hash="0xblock",
        triggered_events=[spent()] if events is None else events,
    )
    substrate.submit_extrinsic.return_value = receipt
    substrate.retrieve_extrinsic_by_hash.return_value = receipt
    subtensor = SimpleNamespace(
        substrate=substrate,
        get_current_block=lambda: 400,
        get_hotkey_owner=lambda _: COLDKEY,
        close=Mock(),
    )
    wallet = SimpleNamespace(
        coldkeypub=SimpleNamespace(ss58_address=COLDKEY), coldkey="COLD_KEY"
    )
    return subtensor, wallet


def pay(subtensor, wallet, before=None):
    return execute_gamma_spend(
        subtensor=subtensor,
        wallet=wallet,
        netuid=80,
        hotkey=HOTKEY,
        amount_gamma="25",
        reference=reference(),
        before_submit=before or Mock(),
    )


def test_payment_journals_the_signed_hash_before_it_broadcasts():
    subtensor, wallet = chain()
    ordered = []

    def before(tx, start):
        assert tx == TX and start == 400
        assert not subtensor.substrate.submit_extrinsic.called
        ordered.append("journal")

    result = pay(subtensor, wallet, before)
    assert result.tx_hash == TX and result.block_number == 401
    assert ordered == ["journal"]
    assert subtensor.substrate.create_signed_extrinsic.call_args.kwargs["era"] == {
        "period": 64,
        "current": 400,
    }
    assert subtensor.substrate.compose_call.call_args.kwargs == {
        "call_module": "SubtensorModule",
        "call_function": "spend_gamma",
        "call_params": {
            "netuid": 80,
            "spends": [[(HOTKEY, 25_000_000_000, reference())]],
        },
    }
    assert subtensor.substrate.submit_extrinsic.call_args.kwargs == {
        "wait_for_inclusion": True,
        "wait_for_finalization": True,
    }


@pytest.mark.parametrize(
    "events",
    [
        [],
        [spent(amount=24_000_000_000)],
        [spent(netuid=81)],
        [spent(spender=HOTKEY)],
        [spent(reference="0x" + "00" * 32)],
    ],
)
def test_successful_transaction_without_exact_spent_event_is_not_payment(events):
    subtensor, wallet = chain(events)
    with pytest.raises(GammaPaymentError, match="no matching payment"):
        pay(subtensor, wallet)


def test_reference_bytes_from_chain_are_normalized():
    subtensor, wallet = chain([spent(reference=list(bytes.fromhex(reference()[2:])))])
    assert pay(subtensor, wallet).tx_hash == TX


def test_unknown_broadcast_retains_the_signed_identity():
    subtensor, wallet = chain()
    subtensor.substrate.submit_extrinsic.side_effect = TimeoutError("connection lost")
    before = Mock()
    with pytest.raises(GammaPaymentError, match="unknown result"):
        pay(subtensor, wallet, before)
    before.assert_called_once_with(TX, 400)


@pytest.mark.parametrize(
    "change,reason",
    [
        (None, "No Gamma deposit"),
        ({"coldkey": HOTKEY}, "another coldkey"),
        ({"spender": HOTKEY}, "own coldkey as spender"),
        ({"balance": 1}, "below the submission fee"),
        (
            {"daily_spend_cap": {"cap": 25_000_000_000, "bucket": [0, 400]}},
            "daily allowance",
        ),
    ],
)
def test_deposit_problems_stop_before_signing(change, reason):
    subtensor, wallet = chain()
    if change is None:
        subtensor.substrate.query.return_value.value = None
    else:
        subtensor.substrate.query.return_value.value.update(change)
    with pytest.raises(GammaPaymentError, match=reason):
        pay(subtensor, wallet)
    assert not subtensor.substrate.create_signed_extrinsic.called
    assert not subtensor.substrate.submit_extrinsic.called


def test_daily_allowance_refills_at_the_chain_rate():
    subtensor, _wallet = chain()
    subtensor.get_current_block = lambda: 7600
    subtensor.substrate.query.return_value.value["daily_spend_cap"] = {
        "cap": 25_000_000_000,
        "bucket": [0, 400],
    }
    assert (
        check_gamma_deposit(
            subtensor,
            netuid=80,
            hotkey=HOTKEY,
            coldkey=COLDKEY,
            amount_units=25_000_000_000,
        )
        == 50_000_000_000
    )


def pending():
    return {
        "network": "finney",
        "backend_url": "https://api.openroboto.ai",
        "genesis_hash": GENESIS,
        "tx_hash": TX,
        "start_block": 400,
        "netuid": 80,
        "hotkey": HOTKEY,
        "coldkey": COLDKEY,
        "amount_gamma": "25",
        "reference": reference(),
        "state": state(),
        "status": "signed",
    }


def test_recovery_reads_the_original_transaction_without_broadcasting():
    subtensor, _wallet = chain()
    subtensor.substrate.get_block.return_value = {
        "extrinsics": [SimpleNamespace(extrinsic_hash=bytes.fromhex(TX[2:]))],
    }
    assert recover_gamma_spend(subtensor, pending()).block_number == 400
    assert not subtensor.substrate.create_signed_extrinsic.called
    assert not subtensor.substrate.submit_extrinsic.called


def test_recovery_not_found_never_signs_a_new_charge():
    subtensor, _wallet = chain()
    subtensor.substrate.get_block.return_value = {"extrinsics": []}
    with pytest.raises(GammaPaymentError, match="No replacement payment"):
        recover_gamma_spend(subtensor, pending())
    assert not subtensor.substrate.create_signed_extrinsic.called
    assert not subtensor.substrate.submit_extrinsic.called


def test_durable_journal_survives_a_corrupt_main_checkpoint(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    gamma_payment._save_journal(
        75, pending() | {"status": "confirmed", "block_number": 401}
    )
    current = {}
    subtensor, _wallet = chain()
    monkeypatch.setattr(gamma_payment, "get_subtensor", lambda _: subtensor)
    assert gamma_payment.resume_gamma_payment(Settings(netuid=80), 75, current)
    assert current["hf_commit"] == COMMIT
    assert current["burn_tx_hash"] == TX
    assert current["gamma_payment_reference"] == reference()
    persisted = json.loads(Path("state/competition_75.json").read_text())
    assert persisted == current


def test_unknown_payment_blocks_a_changed_submission(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    gamma_payment._save_journal(75, pending())
    subtensor, _wallet = chain()
    monkeypatch.setattr(gamma_payment, "get_subtensor", lambda _: subtensor)
    with pytest.raises(GammaPaymentError, match="workspace changed"):
        gamma_payment.resume_gamma_payment(
            Settings(netuid=80), 75, state() | {"hf_commit": "b" * 40}
        )


def test_lock_prevents_two_processes_entering_payment(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with gamma_payment.gamma_submission_lock(75):
        with pytest.raises(GammaPaymentError, match="Another Gamma submit"):
            with gamma_payment.gamma_submission_lock(75):
                pytest.fail("second submit entered the payment section")
    with gamma_payment.gamma_submission_lock(75):
        pass


def live():
    return Competition(
        id=75,
        track="task",
        seq=3,
        label="Task",
        adapter="task",
        status="active",
        params={"fee": dict(FEE)},
    )


def test_gamma_fee_is_not_tao_and_price_drift_refuses_payment():
    competition = live()
    fee = fee_of(competition.params, where="fixture")
    assert fee.kind == GAMMA and fee.amount_tao is None and fee.amount_gamma == "25"
    assert fee.display_amount == "25 Gamma"
    snapshot = Snapshot(competition.model_dump())
    assert judge(snapshot, [competition], datetime.now(UTC)).fee == fee
    changed = copy.deepcopy(competition)
    changed.params["fee"]["amount_gamma"] = "26"
    with pytest.raises(Exception, match="entry fee has changed"):
        judge(snapshot, [changed], datetime.now(UTC))


@pytest.mark.parametrize("amount", [None, True, "NaN", "0", "-1", "0.0000000001"])
def test_invalid_gamma_price_is_not_replaced_with_tao(amount):
    with pytest.raises(ConfigError, match="Invalid Gamma fee"):
        fee_of(
            {"fee": {"kind": "gamma", "amount_gamma": amount, "amount_tao": 5}},
            where="fixture",
        )


def test_confirmation_names_gamma_and_its_deposit(monkeypatch, capsys):
    from openroboto import competition

    monkeypatch.setattr(competition, "_confirmed", lambda _: True)
    confirm_payment(Verdict(live(), fee_of({"fee": FEE}, where="fixture"), "open"), 80)
    output = capsys.readouterr().out
    assert "25 Gamma" in output and "own Gamma deposit" in output
    assert "25 TAO" not in output


def test_doctor_checks_gamma_instead_of_comparing_it_to_tao(monkeypatch):
    from openroboto import chain as chain_module

    settings = Settings.from_mapping(
        {
            "subnet": {"netuid": 80, "hotkey_ss58": HOTKEY},
            "competition": live().model_dump(),
        }
    )
    subtensor, wallet = chain()
    subtensor.get_balance = lambda _: 0.01
    subtensor.close = Mock()
    monkeypatch.setattr(doctor, "_coldkey_address", lambda _: COLDKEY)
    monkeypatch.setattr(chain_module, "get_subtensor", lambda _: subtensor)
    monkeypatch.setattr(chain_module, "open_wallet", lambda _: wallet)
    result = doctor.check_wallet(settings)
    assert result.ok and "50 Gamma" in result.detail and "25 Gamma" in result.detail


def test_command_records_the_payment_then_reuses_it(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    settings = Settings(netuid=80)
    current = state()
    subtensor, wallet = chain()
    subtensor.get_hotkey_owner = lambda _: COLDKEY
    subtensor.close = Mock()
    monkeypatch.setattr(gamma_payment, "get_subtensor", lambda _: subtensor)
    monkeypatch.setattr(gamma_payment, "open_wallet", lambda _: wallet)
    monkeypatch.setattr(gamma_payment, "_ready_to_spend", lambda *a, **kw: True)
    verdict = Verdict(live(), fee_of({"fee": FEE}, where="fixture"), "open")
    assert gamma_payment.perform_gamma(settings, 75, current, verdict)
    assert current["burn_tx_hash"] == TX
    journal = json.loads(Path("state/competition_75.gamma.json").read_text())
    assert journal["status"] == "confirmed"
    assert journal["amount_gamma"] == "25"
    assert gamma_payment.perform_gamma(settings, 75, current, verdict)
    assert subtensor.substrate.submit_extrinsic.call_count == 1


def test_command_recovers_a_timeout_without_another_signature(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    settings = Settings(netuid=80)
    current = state()
    subtensor, wallet = chain()
    subtensor.get_hotkey_owner = lambda _: COLDKEY
    subtensor.close = Mock()
    subtensor.substrate.submit_extrinsic.side_effect = TimeoutError("disconnected")
    monkeypatch.setattr(gamma_payment, "get_subtensor", lambda _: subtensor)
    monkeypatch.setattr(gamma_payment, "open_wallet", lambda _: wallet)
    monkeypatch.setattr(gamma_payment, "_ready_to_spend", lambda *a, **kw: True)
    verdict = Verdict(live(), fee_of({"fee": FEE}, where="fixture"), "open")
    with pytest.raises(GammaPaymentError, match="unknown result"):
        gamma_payment.perform_gamma(settings, 75, current, verdict)
    journal = json.loads(Path("state/competition_75.gamma.json").read_text())
    assert journal["status"] == "signed" and journal["tx_hash"] == TX
    subtensor.substrate.get_block.return_value = {
        "extrinsics": [SimpleNamespace(extrinsic_hash=bytes.fromhex(TX[2:]))]
    }
    assert gamma_payment.resume_gamma_payment(settings, 75, current)
    assert current["burn_tx_hash"] == TX
    assert subtensor.substrate.create_signed_extrinsic.call_count == 1
    assert subtensor.substrate.submit_extrinsic.call_count == 1


def test_proven_skipped_spend_can_retry_after_deposit_is_fixed(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    settings = Settings(netuid=80)
    current = state()
    skipped = spent()
    skipped["event"]["event_id"] = "GammaSpendSkipped"
    skipped["event"]["attributes"]["reason"] = "GammaDepositDailySpendCapExceeded"
    subtensor, wallet = chain([skipped])
    subtensor.get_hotkey_owner = lambda _: COLDKEY
    subtensor.close = Mock()
    monkeypatch.setattr(gamma_payment, "get_subtensor", lambda _: subtensor)
    monkeypatch.setattr(gamma_payment, "open_wallet", lambda _: wallet)
    monkeypatch.setattr(gamma_payment, "_ready_to_spend", lambda *a, **kw: True)
    verdict = Verdict(live(), fee_of({"fee": FEE}, where="fixture"), "open")
    with pytest.raises(GammaPaymentError, match="No Gamma was paid"):
        gamma_payment.perform_gamma(settings, 75, current, verdict)
    journal = json.loads(Path("state/competition_75.gamma.json").read_text())
    assert journal["status"] == "not_spent"
    assert "burn_tx_hash" not in current
    subtensor.substrate.submit_extrinsic.return_value.triggered_events = [spent()]
    assert gamma_payment.perform_gamma(settings, 75, current, verdict)
    assert current["burn_tx_hash"] == TX
    assert subtensor.substrate.submit_extrinsic.call_count == 2


def test_failed_journal_storage_stops_broadcast():
    subtensor, wallet = chain()
    with pytest.raises(GammaPaymentError, match="Nothing was sent or paid"):
        pay(subtensor, wallet, Mock(side_effect=OSError("disk full")))
    assert not subtensor.substrate.submit_extrinsic.called


@pytest.mark.parametrize(
    "field,value",
    [
        ("network", "test"),
        ("backend_url", "https://another-backend.invalid"),
        ("genesis_hash", "0x" + "34" * 32),
        ("netuid", 81),
    ],
)
def test_resume_refuses_another_chain_or_backend(tmp_path, monkeypatch, field, value):
    monkeypatch.chdir(tmp_path)
    value_journal = pending() | {"status": "confirmed", "block_number": 401}
    if field == "netuid":
        value_journal["netuid"] = value
        value_journal["reference"] = gamma_payment_reference(
            netuid=value,
            competition_id=75,
            hotkey=HOTKEY,
            hf_repo_id="author/task",
            hf_commit=COMMIT,
        )
    else:
        value_journal[field] = value
    gamma_payment._save_journal(75, value_journal)
    subtensor, _wallet = chain()
    monkeypatch.setattr(gamma_payment, "get_subtensor", lambda _: subtensor)
    with pytest.raises(GammaPaymentError, match=r"another|genesis"):
        gamma_payment.resume_gamma_payment(Settings(netuid=80), 75, state())
    assert not subtensor.substrate.submit_extrinsic.called


@pytest.mark.parametrize(
    "change", ["commit", "reference", "tx_hash", "block", "missing"]
)
def test_corrupt_journal_cannot_restore_paid_state(tmp_path, monkeypatch, change):
    monkeypatch.chdir(tmp_path)
    journal = pending() | {"status": "confirmed", "block_number": 401}
    if change == "commit":
        journal["state"]["hf_commit"] = "b" * 40
    elif change == "reference":
        journal["reference"] = "0x" + "cd" * 32
    elif change == "tx_hash":
        journal["tx_hash"] = "0xnot-a-transaction"
    elif change == "block":
        journal["block_number"] = 500
    else:
        del journal["coldkey"]
    gamma_payment._save_journal(75, journal)
    current = {}
    with pytest.raises(
        GammaPaymentError, match="Cannot read the Gamma payment journal"
    ):
        gamma_payment.resume_gamma_payment(Settings(netuid=80), 75, current)
    assert current == {}


def test_expiry_requires_the_complete_finalized_era():
    from openroboto.payment.gamma import GammaPaymentNotSpent

    subtensor, _wallet = chain()
    subtensor.get_current_block = lambda: 480
    subtensor.substrate.get_block_number.return_value = 464
    subtensor.substrate.get_block.return_value = {"extrinsics": []}
    with pytest.raises(GammaPaymentNotSpent, match="expired without inclusion"):
        recover_gamma_spend(subtensor, pending())
    blocks = [
        call.kwargs["block_number"]
        for call in subtensor.substrate.get_block.call_args_list
    ]
    assert blocks == list(range(400, 464))
    assert not subtensor.substrate.create_signed_extrinsic.called


def test_best_head_alone_cannot_prove_expiry_or_payment():
    subtensor, _wallet = chain()
    subtensor.get_current_block = lambda: 480
    subtensor.substrate.get_block_number.return_value = 399
    subtensor.substrate.get_block.return_value = {
        "extrinsics": [SimpleNamespace(extrinsic_hash=bytes.fromhex(TX[2:]))]
    }
    with pytest.raises(GammaPaymentError, match="No replacement payment"):
        recover_gamma_spend(subtensor, pending())
    assert not subtensor.substrate.get_block.called
    assert not subtensor.substrate.submit_extrinsic.called


def test_incomplete_finalized_scan_never_allows_another_payment():
    subtensor, _wallet = chain()
    subtensor.substrate.get_block_number.return_value = 464
    subtensor.substrate.get_block.side_effect = lambda *, block_number: (
        None if block_number == 430 else {"extrinsics": []}
    )
    with pytest.raises(GammaPaymentError, match="Cannot read block 430"):
        recover_gamma_spend(subtensor, pending())
    assert not subtensor.substrate.submit_extrinsic.called


def test_paid_but_expired_window_never_sends_a_replacement(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    gamma_payment._save_journal(
        75, pending() | {"status": "confirmed", "block_number": 401}
    )
    subtensor, _wallet = chain()
    subtensor.get_current_block = lambda: 500
    monkeypatch.setattr(gamma_payment, "get_subtensor", lambda _: subtensor)
    with pytest.raises(GammaPaymentError, match="announcement window expired"):
        gamma_payment.resume_gamma_payment(Settings(netuid=80), 75, state())
    assert not subtensor.substrate.submit_extrinsic.called
    assert (
        json.loads(Path("state/competition_75.gamma.json").read_text())["status"]
        == "confirmed"
    )


def test_two_workspaces_share_one_authoritative_payment(tmp_path, monkeypatch):
    first, second = tmp_path / "one", tmp_path / "two"
    first.mkdir()
    second.mkdir()
    subtensor, wallet = chain()
    monkeypatch.setattr(gamma_payment, "get_subtensor", lambda _: subtensor)
    monkeypatch.setattr(gamma_payment, "open_wallet", lambda _: wallet)
    monkeypatch.setattr(gamma_payment, "_ready_to_spend", lambda *a, **kw: True)
    settings = Settings(netuid=80)
    verdict = Verdict(live(), fee_of({"fee": FEE}, where="fixture"), "open")
    first_state, second_state = state(), state()
    monkeypatch.chdir(first)
    assert gamma_payment.perform_gamma(settings, 75, first_state, verdict)
    monkeypatch.chdir(second)
    assert gamma_payment.perform_gamma(settings, 75, second_state, verdict)
    assert first_state["burn_tx_hash"] == second_state["burn_tx_hash"] == TX
    assert subtensor.substrate.create_signed_extrinsic.call_count == 1
    assert subtensor.substrate.submit_extrinsic.call_count == 1
    assert len(list(gamma_payment.SHARED_PAYMENT_ROOT.rglob("*.json"))) == 1


def test_finalized_expired_pending_resumes_through_safe_retry(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    gamma_payment._save_journal(75, pending())
    subtensor, _wallet = chain()
    subtensor.substrate.get_block_number.return_value = 464
    subtensor.substrate.get_block.return_value = {"extrinsics": []}
    monkeypatch.setattr(gamma_payment, "get_subtensor", lambda _: subtensor)
    assert not gamma_payment.resume_gamma_payment(Settings(netuid=80), 75, state())
    saved = json.loads(Path("state/competition_75.gamma.json").read_text())
    assert saved["status"] == "not_spent"
    assert not subtensor.substrate.create_signed_extrinsic.called


def test_spend_shape_encodes_with_the_real_sdk_scale_codec():
    """Finney metadata's BoundedVec is a one-field tuple around its Vec."""
    from scalecodec.base import RuntimeConfigurationObject
    from scalecodec.type_registry import load_type_registry_preset

    hotkey = "5C4hrfjw9DjXZTzV3MwzrrAr9P1MJhSrvWGWqi1eSuyUpnhM"
    units = 7_123_456_789
    ref = "0x" + "00" * 32
    runtime = RuntimeConfigurationObject()
    runtime.update_type_registry(load_type_registry_preset("core"))
    runtime.update_type_registry_types(
        {
            "GammaEntry": {
                "type": "tuple",
                "type_mapping": ["AccountId", "u64", "[u8; 32]"],
            },
            "BoundedGammaSpends": {
                "type": "tuple",
                "type_mapping": ["Vec<GammaEntry>"],
            },
        }
    )
    subtensor, wallet = chain([spent(hotkey=hotkey, amount=units, reference=ref)])
    encoded = []

    def compose(*, call_module, call_function, call_params):
        assert call_module == "SubtensorModule" and call_function == "spend_gamma"
        params = runtime.create_scale_object("BoundedGammaSpends")
        body = params.encode(call_params["spends"]).to_hex()[2:]
        encoded.append(
            "0x07a4" + call_params["netuid"].to_bytes(2, "little").hex() + body
        )
        return "SCALE_CALL"

    subtensor.substrate.compose_call.side_effect = compose
    execute_gamma_spend(
        subtensor=subtensor,
        wallet=wallet,
        netuid=80,
        hotkey=hotkey,
        amount_gamma="7.123456789",
        reference=ref,
        before_submit=Mock(),
    )
    assert encoded == [
        "0x07a45000040000000000000000000000000000000000000000000000000000"
        "000000000000155397a801000000000000000000000000000000000000000000"
        "0000000000000000000000000000"
    ]
    with pytest.raises(ValueError, match="Provided value is not a list"):
        runtime.create_scale_object("BoundedGammaSpends").encode([(hotkey, units, ref)])


def test_shared_lock_stops_a_second_directory_during_broadcast(tmp_path, monkeypatch):
    first, second = tmp_path / "one", tmp_path / "two"
    first.mkdir()
    second.mkdir()
    subtensor, wallet = chain()
    monkeypatch.setattr(gamma_payment, "get_subtensor", lambda _: subtensor)
    monkeypatch.setattr(gamma_payment, "open_wallet", lambda _: wallet)
    monkeypatch.setattr(gamma_payment, "_ready_to_spend", lambda *a, **kw: True)
    settings = Settings(netuid=80)
    verdict = Verdict(live(), fee_of({"fee": FEE}, where="fixture"), "open")
    receipt = subtensor.substrate.submit_extrinsic.return_value

    def broadcast(*a, **kw):
        monkeypatch.chdir(second)
        try:
            with pytest.raises(GammaPaymentError, match="another workspace"):
                gamma_payment.perform_gamma(settings, 75, state(), verdict)
        finally:
            monkeypatch.chdir(first)
        return receipt

    subtensor.substrate.submit_extrinsic.side_effect = broadcast
    monkeypatch.chdir(first)
    assert gamma_payment.perform_gamma(settings, 75, state(), verdict)
    assert subtensor.substrate.create_signed_extrinsic.call_count == 1
    assert subtensor.substrate.submit_extrinsic.call_count == 1


def test_same_backend_slot_in_another_repo_cannot_pay_again(tmp_path, monkeypatch):
    first, second = tmp_path / "one", tmp_path / "two"
    first.mkdir()
    second.mkdir()
    subtensor, wallet = chain()
    monkeypatch.setattr(gamma_payment, "get_subtensor", lambda _: subtensor)
    monkeypatch.setattr(gamma_payment, "open_wallet", lambda _: wallet)
    monkeypatch.setattr(gamma_payment, "_ready_to_spend", lambda *a, **kw: True)
    settings = Settings(netuid=80)
    verdict = Verdict(live(), fee_of({"fee": FEE}, where="fixture"), "open")
    monkeypatch.chdir(first)
    assert gamma_payment.perform_gamma(settings, 75, state(), verdict)
    monkeypatch.chdir(second)
    with pytest.raises(
        GammaPaymentError, match="already has a Gamma payment for author/task"
    ):
        gamma_payment.perform_gamma(
            settings, 75, state() | {"hf_repo_id": "other/clone"}, verdict
        )
    assert subtensor.substrate.create_signed_extrinsic.call_count == 1
    assert subtensor.substrate.submit_extrinsic.call_count == 1
