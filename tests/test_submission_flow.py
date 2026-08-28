"""Submission pipeline: upload -> burn -> announce.

Every step on this path either costs money or is irreversible, so what is tested is
**ordering and preconditions**: not one cent is burned if preflight fails; nothing is
burned twice; the commit carried in the announcement must not be empty.
Chain and HF are entirely faked, so these tests need no network, wallet or GPU.
"""

from __future__ import annotations

import argparse
import io
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from openroboto_protocol.commitment import CommitmentPayload, decode, encode
from openroboto_protocol.schemas import Competition

from openroboto import competition as competition_module
from openroboto.chain.commitment import SubmitResult
from openroboto.cli import main
from openroboto.commands import announce as announce_command
from openroboto.commands import burn as burn_command
from openroboto.commands import submit as submit_command
from openroboto.commands import upload as upload_module
from openroboto.config import Settings
from openroboto.huggingface import UploadResult
from openroboto.huggingface import tree as tree_module
from openroboto.preflight import payload_size, payload_track
from openroboto.round_state import (
    announced_commit,
    competition_id,
    load_state,
    save_state,
)

HOTKEY = "5" + "M" * 47
COMMIT = "a" * 40
#: The coldkey that owns `HOTKEY` on the fake chain, and the one the fake wallet
#: pays from. The backend compares those two before it accepts a fee, so every
#: payment fake here has to be able to answer both halves of that comparison.
OWNER_COLDKEY = "5Gw3s7q4QLkSWwknsiPtjujPv3XM4Trxi5d4PgKMMk3gfGTE"


def _settings() -> Settings:
    """A workspace with no competition section -- what `init` wrote before
    seasons existed, and what nothing can pay with any more."""
    return Settings.from_mapping(
        {"subnet": {"netuid": 80, "network": "finney", "hotkey_ss58": HOTKEY}}
    )


def _uploaded_state() -> dict[str, Any]:
    return {
        "hf_repo_id": "kyleab/pi05-abcdefghijkl",
        "hf_url": "https://huggingface.co/kyleab/pi05-abcdefghijkl",
        "hf_commit": COMMIT,
        "hotkey_ss58": HOTKEY,
    }


class _FakeSubtensor:
    closed = False

    def get_current_block(self) -> int:
        return 8_888_888

    def get_block_hash(self, block: int) -> str:
        return "0x" + "c" * 64

    def get_hotkey_owner(self, hotkey_ss58: str) -> str:
        """A chain on which this workspace's wallet does own its hotkey.

        The interesting case is the other one, and it has its own test: an owner
        that does not match is `fee_payer_not_owner`, which is a rejection with
        the fee already spent.
        """
        return OWNER_COLDKEY

    def close(self) -> None:
        self.closed = True


class _FakeWallet:
    class hotkey:
        ss58_address = HOTKEY

    class coldkeypub:
        ss58_address = OWNER_COLDKEY


# ─── burn ────────────────────────────────────────────────────


def test_burn_spends_nothing_when_preflight_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)

    def _explode(*args: Any, **kwargs: Any) -> None:
        raise AssertionError(
            "connected to the chain even though preflight failed -- this is exactly "
            "the path that burns TAO for nothing"
        )

    monkeypatch.setattr(burn_command, "get_subtensor", _explode)

    assert burn_command.perform_burn(_settings(), 1, {}, _verdict()) is False


def test_burn_records_tx_and_block_in_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    from openroboto.payment import BurnReceipt

    subtensor = _FakeSubtensor()
    monkeypatch.setattr(burn_command, "get_subtensor", lambda network: subtensor)
    monkeypatch.setattr(burn_command, "open_wallet", lambda settings: _FakeWallet())

    seen: dict[str, Any] = {}

    def _burn(**kwargs: Any) -> BurnReceipt:
        seen.update(kwargs)
        return BurnReceipt(tx_hash="0x" + "d" * 64, block_number=8_888_880)

    monkeypatch.setattr(burn_command, "execute_stake_burn", _burn)

    state = _uploaded_state()
    assert burn_command.perform_burn(_settings(), 1, state, _verdict(0.1)) is True
    # the amount is the verdict's, i.e. the season's own `params.fee`
    assert seen["amount_tao"] == 0.1
    assert seen["netuid"] == 80
    assert state["burn_tx_hash"].startswith("0x")
    assert state["burn_block"] == 8_888_880
    assert subtensor.closed


# ─── announce ────────────────────────────────────────────────


def _capture_announcement(
    monkeypatch: pytest.MonkeyPatch, ok: bool = True, confirmed: bool = True
) -> list[CommitmentPayload]:
    captured: list[CommitmentPayload] = []

    def _submit(subtensor: Any, wallet: Any, netuid: int, payload: CommitmentPayload):
        captured.append(payload)
        return SubmitResult(
            ok=ok,
            extrinsic_hash="ff",
            block_height=1,
            extrinsic_index=2,
            fee_tao=0.0,
            confirmed=confirmed,
        )

    monkeypatch.setattr(
        announce_command, "get_subtensor", lambda network: _FakeSubtensor()
    )
    monkeypatch.setattr(announce_command, "open_wallet", lambda settings: _FakeWallet())
    monkeypatch.setattr(announce_command, "submit_announcement", _submit)
    return captured


def test_announce_fills_commit_from_state_when_url_has_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The old code only scraped the commit out of hf_url; when the URL carries no
    `/commit/`, the on-chain `c` is an empty string, and a backend with no commit means
    this submission is void (with the TAO already burned)."""
    monkeypatch.chdir(tmp_path)
    captured = _capture_announcement(monkeypatch)

    state = _uploaded_state()
    state["burn_tx_hash"] = "0x" + "d" * 64
    state["burn_block"] = 8_888_880

    assert announce_command.perform_announce(_settings(), 1, state) is True
    payload = captured[0]
    assert payload.hf_commit == COMMIT
    assert payload.burn_block == 8_888_880
    assert state["step"] == "announce"


def test_announce_payload_round_trips_through_the_protocol_codec(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The bytes are produced by the protocol package and the backend decodes them with
    the same module -- this proves both ends line up."""
    monkeypatch.chdir(tmp_path)
    captured = _capture_announcement(monkeypatch)

    state = _uploaded_state()
    state["burn_tx_hash"] = "0x" + "d" * 64
    state["burn_block"] = 8_888_880
    announce_command.perform_announce(_settings(), 1, state)

    decoded = decode(encode(captured[0])).payload
    assert decoded.hotkey_ss58 == HOTKEY
    assert decoded.hf_repo_id == "kyleab/pi05-abcdefghijkl"
    assert decoded.burn_tx_hash == "0x" + "d" * 64  # 0x is put back when decoding
    assert decoded.block_hash == "c" * 64  # 0x is stripped when encoding


def test_announce_failure_tells_the_miner_not_to_burn_again(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    _capture_announcement(monkeypatch, ok=False)

    state = _uploaded_state()
    state["burn_tx_hash"] = "0x" + "d" * 64
    assert announce_command.perform_announce(_settings(), 1, state) is False
    assert "do not burn again" in capsys.readouterr().err


# ─── the amount must come from the season, never be guessed ──────────────────


def test_the_payment_cannot_be_called_without_a_confirmed_season() -> None:
    """🔴 The gate is the **signature**, which is why it is asserted on directly.

    `verdict` used to default to `None`, and every caller that forgot it fell
    through to a subnet-wide rate: control.json's, or whatever had been typed
    into `payment.burn_rate_tao`. Either one is an amount with no season attached
    to it, and a fee paid that way is filed under whichever season the backend
    defaults to -- non-refundably. Give this parameter a default again and that
    hole reopens with no test failing anywhere else, because the failure is a
    call that *type-checks*.
    """
    import inspect

    verdict = inspect.signature(burn_command.perform_burn).parameters["verdict"]
    assert verdict.default is inspect.Parameter.empty


# ─── burn validity window (backend: 50 blocks; over it means rejected, no refund) ────
#
# The decision must match `prototype/backend/scanner/burn_verify.py:68-75` exactly;
# being any stricter blocks submissions that would have passed.


def _announce_with_burn_block(
    monkeypatch: pytest.MonkeyPatch, burn_block: int, confirmed: bool = True
) -> tuple[bool, list[CommitmentPayload]]:
    captured = _capture_announcement(monkeypatch, confirmed=confirmed)
    state = _uploaded_state()
    state["burn_tx_hash"] = "0x" + "d" * 64
    state["burn_block"] = burn_block
    ok = announce_command.perform_announce(_settings(), 1, state)
    return ok, captured


CURRENT_BLOCK = 8_888_888  # _FakeSubtensor.get_current_block()
WINDOW = 50  # Settings.burn_block_window; measured from the production backend.yaml


def test_announce_refuses_once_the_burn_window_has_passed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Past the window, do not announce at all -- the backend will certainly reject, so
    sending it only pays a fee for nothing."""
    monkeypatch.chdir(tmp_path)
    ok, captured = _announce_with_burn_block(monkeypatch, CURRENT_BLOCK - WINDOW - 1)

    assert ok is False
    assert captured == []  # not a single commitment was sent
    out = capsys.readouterr()
    assert "51" in out.err and "50" in out.err  # distance and window; both must be said

    # 🔴 The last thing printed must not claim a commitment is being sent.
    #
    # These lines used to run in the other order: "📡 committing on chain" was
    # printed before the window was checked, so a refused announce ended with
    # that line on screen while no extrinsic existed. The exit code was already
    # 1 -- correct for a script, useless for a person, who reads the last line.
    # Cost ten minutes of hunting through the chain, the database and the ingest
    # logs for a commitment that had been deliberately not sent.
    assert "committing on chain" not in out.out, (
        "refused to announce, but the output still says it is committing"
    )
    assert "nothing was sent on chain" in out.out


def test_announce_allows_exactly_at_the_window_edge(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The backend only rejects on `block_diff > window` -- exactly 50 is **allowed**.

    This boundary is pinned: writing `>=` would block submissions the backend would
    have accepted.
    """
    monkeypatch.chdir(tmp_path)
    ok, captured = _announce_with_burn_block(monkeypatch, CURRENT_BLOCK - WINDOW)

    assert ok is True
    assert len(captured) == 1


def test_announce_window_check_is_symmetric(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The backend uses `abs(burn_block - commit_block)`, so a burn after the commit
    counts as distance too."""
    monkeypatch.chdir(tmp_path)
    ok, captured = _announce_with_burn_block(monkeypatch, CURRENT_BLOCK + WINDOW + 1)

    assert ok is False
    assert captured == []


def test_announce_skips_the_window_check_when_the_burn_block_is_unknown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With `burn_block=0` the backend skips this check entirely, so we skip it too --
    we must not be stricter than the backend."""
    monkeypatch.chdir(tmp_path)
    ok, captured = _announce_with_burn_block(monkeypatch, 0)

    assert ok is True
    assert len(captured) == 1


# ─── "thought it was on chain, but it was not" ───────────────────────────────


def test_unconfirmed_submission_does_not_invent_a_block_reference() -> None:
    """No receipt means no block number.

    When there was no receipt, the old implementation filled `block_height` with
    `get_current_block()`, so `extrinsic_ref` printed a perfectly normal-looking
    `6123456-0` -- from which the miner concluded the announcement was on chain, while
    it may never have been included at all.
    """
    unconfirmed = SubmitResult(
        ok=True,
        extrinsic_hash="ff",
        block_height=CURRENT_BLOCK,  # even with a value, unconfirmed must not be used
        # as a reference
        extrinsic_index=0,
        fee_tao=0.0,
        confirmed=False,
    )
    # only assert "this is not something that looks like a real block reference" --
    # the exact wording gets translated.
    assert "-" not in unconfirmed.extrinsic_ref
    assert str(CURRENT_BLOCK) not in unconfirmed.extrinsic_ref

    confirmed = SubmitResult(
        ok=True,
        extrinsic_hash="ff",
        block_height=CURRENT_BLOCK,
        extrinsic_index=3,
        fee_tao=0.0,
        confirmed=True,
    )
    assert confirmed.extrinsic_ref == f"{CURRENT_BLOCK}-3"


def test_announce_does_not_claim_on_chain_without_a_block_number(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The SDK reports success but gives no block number: do not treat it as a failure
    (the transaction really was sent), but do not claim "on chain" either."""
    monkeypatch.chdir(tmp_path)
    ok, _ = _announce_with_burn_block(monkeypatch, CURRENT_BLOCK - 8, confirmed=False)

    assert ok is True
    out = capsys.readouterr().out
    assert "commitment submitted" in out and "commitment on chain" not in out


def test_parse_extrinsic_result_confirms_only_with_a_real_block() -> None:
    """The only source of `confirmed` is the block number in the receipt, not the
    SDK's success boolean."""
    from openroboto.chain.commitment import parse_extrinsic_result

    class _Receipt:
        extrinsic_hash = "0xabc"
        block_number = 8_888_800
        extrinsic_idx = 4

    class _Success:
        is_success = True
        extrinsic_hash = "0xabc"
        extrinsic_receipt = _Receipt()

    class _SuccessNoReceipt:
        is_success = True
        extrinsic_hash = "0xabc"
        extrinsic_receipt = None

    assert parse_extrinsic_result(_Success()).confirmed is True
    assert parse_extrinsic_result(_Success()).extrinsic_ref == "8888800-4"

    no_receipt = parse_extrinsic_result(_SuccessNoReceipt())
    assert no_receipt.ok is True  # the transaction went out
    assert no_receipt.confirmed is False  # but we do not know which block it is in
    assert no_receipt.block_height == 0  # do not invent a block number


def _payload() -> CommitmentPayload:
    return CommitmentPayload(
        hotkey_ss58=HOTKEY,
        block_hash="c" * 64,
        hf_commit=COMMIT,
        round_num=1,
        hf_repo_id="kyleab/pi05-abcdefghijkl",
        burn_tx_hash="0x" + "d" * 64,
        burn_block=8_888_880,
    )


def _patch_publish(monkeypatch: pytest.MonkeyPatch, fn: Any) -> None:
    import sys
    import types

    fake = types.ModuleType("bittensor.core.extrinsics.serving")
    fake.publish_metadata_extrinsic = fn  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "bittensor.core.extrinsics.serving", fake)


def test_submit_announcement_reports_unknown_on_rpc_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The RPC drops while waiting for inclusion: the conclusion is "unknown", not
    "failed".

    The transaction may still be included, and falsely reporting failure makes the
    miner think they have to start over.
    """
    from openroboto.chain import commitment as commitment_module

    def _boom(**kwargs: Any) -> None:
        raise TimeoutError("rpc gone")

    _patch_publish(monkeypatch, _boom)

    result = commitment_module.submit_announcement(object(), object(), 80, _payload())
    assert result.ok is False
    assert result.confirmed is False


def test_submit_announcement_does_not_swallow_our_own_bugs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wrong call signature must blow up, and must not be reported as "unknown".

    In that case **nothing at all was sent**. Reporting "unknown" sends the miner off
    to check status and wait while the 50-block burn window drains away -- one bug of
    ours turns into a miner's TAO.
    """
    from openroboto.chain import commitment as commitment_module

    def _wrong_signature(**kwargs: Any) -> None:
        raise TypeError("unexpected keyword argument 'data_type'")

    _patch_publish(monkeypatch, _wrong_signature)

    with pytest.raises(TypeError):
        commitment_module.submit_announcement(object(), object(), 80, _payload())


# ─── submit ──────────────────────────────────────────────────


def test_submit_skips_a_finished_round(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    save_state(1, {"step": "announce", "status": "completed"})
    monkeypatch.setattr(
        submit_command.Settings, "load", staticmethod(lambda path: _settings())
    )

    def _explode(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("a finished round must not run through the pipeline again")

    monkeypatch.setattr(submit_command, "perform_upload", _explode)
    args = argparse.Namespace(config="miner.yaml", round=1, output_dir="", force=False)
    assert submit_command.run(args) == 0


def test_submit_reuses_an_existing_burn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Rerunning after an upload died on the network must not burn a second time."""
    monkeypatch.chdir(tmp_path)
    state = _uploaded_state()
    state.update({"burn_tx_hash": "0x" + "d" * 64, "burn_block": 8_888_880})
    save_state(2, state)

    monkeypatch.setattr(
        submit_command.Settings, "load", staticmethod(lambda path: _settings())
    )
    monkeypatch.setattr(submit_command, "perform_upload", lambda *a, **k: None)

    def _explode(*args: Any, **kwargs: Any) -> None:
        raise AssertionError(
            "the checkpoint already holds a burn_tx; it must not burn again"
        )

    monkeypatch.setattr(submit_command, "perform_burn", _explode)
    monkeypatch.setattr(submit_command, "perform_announce", lambda *a, **k: True)

    args = argparse.Namespace(config="miner.yaml", round=2, output_dir="", force=False)
    assert submit_command.run(args) == 0


def test_submit_force_clears_the_previous_burn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    state = _uploaded_state()
    state.update(
        {"step": "announce", "status": "completed", "burn_tx_hash": "0x" + "d" * 64}
    )
    save_state(3, state)

    _, paid = _submitting(monkeypatch, _season_settings())

    args = argparse.Namespace(config="miner.yaml", round=3, output_dir="", force=True)
    assert submit_command.run(args) == 0
    assert paid == [0.25]


# ─── the gate before the money ───────────────────────────────
#
# Skipping `openroboto check` must not skip this, `--force` must not skip it,
# and there is no flag that does. Every case below asserts on the payment
# function's **call count**, because that is the only assertion that
# distinguishes "refused" from "refused after paying".


def _season_settings(**fee: Any) -> Settings:
    """A config `init` wrote for the LingBot simulation season."""
    return Settings.from_mapping(
        {
            "subnet": {"netuid": 80, "network": "finney", "hotkey_ss58": HOTKEY},
            "competition": {
                "id": 2,
                "track": "sim",
                "seq": 2,
                "label": "LingBot-VLA 2.0",
                "adapter": "sim_lingbot",
                "params": {
                    "fee": {"kind": "burn", "amount_tao": 0.25, "coldkey": None, **fee}
                },
            },
        }
    )


FEE_COLDKEY = "5Feqsy76Do37q6NaKkGnu4191g2gog85rLvNdx3iVHLKugtD"


def _real_season_settings() -> Settings:
    """A config `init` wrote for the xArm 6 season -- the one that pays by
    transfer. Its `base_model_family` is what picks the rule book; the adapter
    name says `xarm6`, which is hardware and names no base model at all."""
    return Settings.from_mapping(
        {
            "environment": "dev",
            "subnet": {"netuid": 313, "network": "test", "hotkey_ss58": HOTKEY},
            "competition": {
                "id": 3,
                "track": "real",
                "seq": 1,
                "label": "xArm 6",
                "adapter": "real_xarm6",
                "base_model_family": "openpi",
                "params": {
                    "fee": {
                        "kind": "transfer",
                        "amount_tao": 2.0,
                        "coldkey": FEE_COLDKEY,
                    }
                },
            },
        }
    )


def _live_row(**overrides: Any) -> Competition:
    """The row the backend serves for the LingBot simulation season.

    A real `Competition` rather than a namespace, because the gates now read it
    for more than its label: the layout gate picks this repository's rule book
    off `adapter` / `base_model_family` / `params`, and a stand-in that answers
    those with whatever the test happened to set would be checking the fake.
    """
    row: dict[str, Any] = {
        "id": 2,
        "track": "sim",
        "seq": 2,
        "label": "LingBot-VLA 2.0",
        "adapter": "sim_lingbot",
        "status": "active",
        "params": {"fee": {"kind": "burn", "amount_tao": 0.25, "coldkey": None}},
    }
    return Competition.model_validate(row | overrides)


def _real_verdict() -> Any:
    """What the season check hands back for that season -- carrying the address,
    which is the field the burn's verdict has no use for and this one cannot do
    without."""
    return SimpleNamespace(
        live=_live_row(
            id=3,
            track="real",
            seq=1,
            label="xArm 6",
            adapter="real_xarm6",
            base_model_family="openpi",
            params={
                "fee": {"kind": "transfer", "amount_tao": 2.0, "coldkey": FEE_COLDKEY}
            },
        ),
        kind="transfer",
        amount_tao=2.0,
        cid=3,
        fee=SimpleNamespace(kind="transfer", amount_tao=2.0, coldkey=FEE_COLDKEY),
    )


def _verdict(amount_tao: float = 0.25, cid: int = 2, kind: str = "burn") -> Any:
    """What `competition.resolve_competition` hands back.

    It is the **only** evidence that the season was confirmed in this run, which
    is why `perform_burn` takes it as an argument rather than reading an amount
    off `Settings`: an amount can be typed into miner.yaml by hand, and a number
    says how much, never which competition.
    """
    return SimpleNamespace(
        live=_live_row(id=cid),
        kind=kind,
        amount_tao=amount_tao,
        cid=cid,
    )


#: A LingBot repository the rules accept: two shards at the top, the config the
#: layout names, the index that lists them. Written as a HuggingFace listing
#: (`type` / `path` / `size`) rather than as files on disk, because the listing
#: is what the gate judges and what the backend judges.
#:
#: The shards carry `lfs.oid` because that is what HuggingFace really returns
#: for them and it is what the fingerprint is made of; a listing without it is a
#: repository holding no weights, which has its own case below.
GOOD_TREE: list[dict[str, Any]] = [
    {"type": "file", "path": ".gitattributes", "size": 1797},
    {"type": "directory", "path": "unused"},
    {"type": "file", "path": "config.json", "size": 31},
    {"type": "file", "path": "model.safetensors.index.json", "size": 92_000},
    {
        "type": "file",
        "path": "model-00001-of-00002.safetensors",
        "size": 6_000_000_000,
        "lfs": {"oid": "1" * 64},
    },
    {
        "type": "file",
        "path": "model-00002-of-00002.safetensors",
        "size": 5_000_000_000,
        "lfs": {"oid": "2" * 64},
    },
]


def _roster(*entries: Any) -> Any:
    """One page of a season's entry list, as `fetch_roster` returns it."""
    return SimpleNamespace(data=list(entries))


def _entry(hf_commit: str, counts_as_submitted: bool = True) -> Any:
    """One roster row, holding (or not holding) its dedup slot.

    🔴 `counts_as_submitted` is the backend's **conclusion**, not its `status`:
    a submission pushed aside by a later one still holds the slot while reading
    `rejected`, and this side is deliberately not in a position to tell.
    """
    return SimpleNamespace(
        hotkey=HOTKEY,
        hf_commit=hf_commit,
        counts_as_submitted=counts_as_submitted,
    )


def _submitting(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    tree: list[dict[str, Any]] | None = None,
    roster: Any = None,
) -> tuple[list[Any], list[Any]]:
    """Wire `submit` up to fakes and return (season checks, payment calls).

    `tree` is the HuggingFace listing the layout gate judges; it defaults to a
    repository that passes, because these cases are about the *season* gate. The
    layout gate has its own section further down, and so does the dedup gate,
    whose `roster` defaults to an entry list this hotkey is not on.
    """
    monkeypatch.setattr(
        submit_command.Settings, "load", staticmethod(lambda path: settings)
    )
    monkeypatch.setattr(submit_command, "perform_upload", lambda *a, **k: None)
    monkeypatch.setattr(submit_command, "perform_announce", lambda *a, **k: True)
    monkeypatch.setattr(
        submit_command,
        "fetch_tree",
        lambda repo, revision, token="": GOOD_TREE if tree is None else tree,
    )
    monkeypatch.setattr(
        submit_command,
        "fetch_roster",
        lambda *a, **k: _roster() if roster is None else roster,
    )
    # The prompt is a conversation with a miner and has nothing to add to these
    # cases; that it is asked **last**, after every gate that could still
    # refuse, has its own test.
    monkeypatch.setattr(submit_command, "confirm_payment", lambda verdict: None)

    checked: list[Any] = []
    paid: list[Any] = []

    def _resolve(cfg: Settings, snapshot: Any, now: Any) -> Any:
        checked.append(snapshot)
        return _verdict()

    def _burn(
        cfg: Settings,
        round_num: int,
        state: dict[str, Any],
        verdict: Any = None,
    ) -> bool:
        # What the payment was told to pay -- `None` if it was handed no verdict
        # at all, which is the case that must never reach the chain.
        paid.append(None if verdict is None else verdict.amount_tao)
        state["burn_tx_hash"] = "0x" + "e" * 64
        return True

    def _transfer(
        cfg: Settings,
        round_num: int,
        state: dict[str, Any],
        verdict: Any = None,
    ) -> bool:
        # Tagged, unlike the burn: the failure this guards against is a season
        # paid the *other* way, and an untagged amount cannot tell the two apart.
        paid.append(("transfer", None if verdict is None else verdict.amount_tao))
        state["burn_tx_hash"] = "0x" + "f" * 64
        return True

    monkeypatch.setattr(submit_command, "resolve_competition", _resolve)
    monkeypatch.setattr(submit_command, "perform_burn", _burn)
    monkeypatch.setattr(submit_command, "perform_transfer", _transfer)
    return checked, paid


def test_submit_checks_the_competition_even_when_check_was_skipped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Also the "the gate is not welded shut" case: a repository the rules
    accept goes all the way through and pays."""
    monkeypatch.chdir(tmp_path)
    save_state(4, _uploaded_state())
    checked, paid = _submitting(monkeypatch, _season_settings())

    args = argparse.Namespace(config="miner.yaml", round=4, output_dir="", force=False)
    assert submit_command.run(args) == 0
    assert len(checked) == 1
    # and the payment was handed this run's verdict, carrying the season's own
    # fee -- not a subnet-wide rate, and not a number left lying in the config
    assert paid == [0.25]


def test_force_does_not_skip_the_competition_check(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`--force` means "burn again", not "burn without looking"."""
    monkeypatch.chdir(tmp_path)
    state = _uploaded_state()
    state.update({"burn_tx_hash": "0x" + "d" * 64})
    save_state(5, state)
    checked, _ = _submitting(monkeypatch, _season_settings())

    args = argparse.Namespace(config="miner.yaml", round=5, output_dir="", force=True)
    assert submit_command.run(args) == 0
    assert len(checked) == 1


def test_a_failed_check_spends_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    save_state(6, _uploaded_state())
    _, paid = _submitting(monkeypatch, _season_settings())

    def _refuse(*args: Any, **kwargs: Any) -> Any:
        raise submit_command.PrecheckFailed("the season closed")

    monkeypatch.setattr(submit_command, "resolve_competition", _refuse)
    monkeypatch.setattr(
        submit_command,
        "perform_announce",
        lambda *a, **k: pytest.fail("announced without paying"),
    )

    args = argparse.Namespace(config="miner.yaml", round=6, output_dir="", force=False)
    assert submit_command.run(args) == 1
    assert paid == []


def test_a_season_paid_by_transfer_is_not_quietly_burned_instead(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """🔴 Burning the right amount the wrong way spends it and still leaves the
    submission unpaid: `add_stake_burn` has no recipient at all, so the season's
    collection address never sees a Rao of it and the fee is not refundable.

    The branch is on `params.fee.kind` as the backend served it in this run --
    not on the track, not on the adapter. Those are two more names for the same
    fact, and they disagree on the first season that breaks the pattern, while
    paying.
    """
    monkeypatch.chdir(tmp_path)
    save_state(7, _uploaded_state())
    _, paid = _submitting(monkeypatch, _season_settings())
    monkeypatch.setattr(
        submit_command,
        "resolve_competition",
        lambda *a, **k: _verdict(amount_tao=2.0, cid=3, kind="transfer"),
    )

    args = argparse.Namespace(config="miner.yaml", round=7, output_dir="", force=False)
    assert submit_command.run(args) == 0
    assert paid == [("transfer", 2.0)]


def test_a_config_from_before_competitions_is_refused_before_it_uploads(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """🔴 The reversal: this path used to pay at control.json's subnet-wide rate.

    Such a workspace cannot say which season it is entering, so the fee it paid
    was filed under whichever season the backend defaults to -- the archived π0.5
    one -- and it is not refunded. `openroboto init` has not produced a config
    like this since seasons existed, so refusing it costs installs from before
    the rebuild, which are out of support (ADR 05).

    The refusal lands **before the upload**: the run cannot end in a payment
    either way, and finding that out after several gigabytes buys nothing. The
    message has to name the command that repairs the file, because it is all the
    miner has.
    """
    monkeypatch.chdir(tmp_path)
    save_state(8, _uploaded_state())
    checked, paid = _submitting(monkeypatch, _settings())
    monkeypatch.setattr(
        submit_command,
        "perform_upload",
        lambda *a, **k: pytest.fail("pushed gigabytes for a run that cannot pay"),
    )

    args = argparse.Namespace(config="miner.yaml", round=8, output_dir="", force=False)
    assert submit_command.run(args) == 1
    assert (checked, paid) == ([], [])
    assert "openroboto init --refresh" in capsys.readouterr().err


def test_a_paid_round_still_announces_without_a_competition_section(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The one exemption to the refusal above, and the reason it is worth having.

    If the fee is already gone, the only thing that can still make it count is
    the commitment. Refusing here would turn an unsupported config into a total
    loss -- money spent, nothing on chain -- which is strictly worse than the
    rejection it was heading for anyway.
    """
    monkeypatch.chdir(tmp_path)
    state = _uploaded_state()
    state["burn_tx_hash"] = "0x" + "e" * 64
    state["burn_block"] = 8_888_880
    save_state(12, state)
    _, paid = _submitting(monkeypatch, _settings())

    args = argparse.Namespace(config="miner.yaml", round=12, output_dir="", force=False)
    assert submit_command.run(args) == 0
    assert paid == []  # not paid a second time


def test_the_fee_that_is_burned_is_the_one_the_verdict_carries(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The amount reaching the chain comes from the row the backend just served,
    and it is **not** written back onto `Settings` on the way -- that field holds
    the subnet-wide rate, and a season's figure sitting in it is a number nobody
    downstream can attribute."""
    monkeypatch.chdir(tmp_path)
    settings = _season_settings()
    monkeypatch.setattr(burn_command, "get_subtensor", lambda network: _FakeSubtensor())
    monkeypatch.setattr(burn_command, "open_wallet", lambda cfg: _FakeWallet())
    burned: dict[str, Any] = {}

    def _burn(**kwargs: Any) -> Any:
        burned.update(kwargs)
        return SimpleNamespace(tx_hash="0x" + "f" * 64, block_number=1)

    monkeypatch.setattr(burn_command, "execute_stake_burn", _burn)
    state = _uploaded_state()
    state["competition_id"] = 2
    save_state(9, state)

    assert burn_command.perform_burn(settings, 9, state, verdict=_verdict(0.25)) is True
    assert burned["amount_tao"] == 0.25
    assert settings.burn_rate_tao is None


def test_the_transfer_goes_to_the_address_the_verdict_carries(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """🔴 The destination comes from the row the backend served seconds ago, not
    from `miner.yaml`.

    `judge()` refuses a season whose address is null and refuses one whose
    address moved since `init`; reading the snapshot here instead would turn
    both of those into advice, on the one value where being wrong puts a
    non-refundable fee into a stranger's account. The amount travels the same
    way and for the same reason as the burn's.
    """
    monkeypatch.chdir(tmp_path)
    settings = _real_season_settings()
    monkeypatch.setattr(burn_command, "get_subtensor", lambda network: _FakeSubtensor())
    monkeypatch.setattr(burn_command, "open_wallet", lambda cfg: _FakeWallet())
    sent: dict[str, Any] = {}

    def _transfer(**kwargs: Any) -> Any:
        sent.update(kwargs)
        return SimpleNamespace(tx_hash="0x" + "a" * 64, block_number=4242)

    monkeypatch.setattr(burn_command, "execute_transfer", _transfer)
    state = _uploaded_state()
    state["competition_id"] = 3
    # The real track puts the fingerprint on chain because the repository may be
    # private -- without it the self-check refuses before any money moves.
    state["model_hash"] = "9" * 64
    save_state(21, state)

    assert (
        burn_command.perform_transfer(settings, 21, state, verdict=_real_verdict())
        is True
    )
    assert sent["amount_tao"] == 2.0
    assert sent["dest_coldkey"] == FEE_COLDKEY
    # and the proof is in the checkpoint under the keys `announce` reads --
    # both tracks put it on chain as `b` / `bb` (spec 10 §3.5)
    assert load_state(21)["burn_tx_hash"] == "0x" + "a" * 64
    assert load_state(21)["burn_block"] == 4242


def test_a_transfer_is_not_sent_when_the_payload_would_not_encode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The pre-spend self-check guards both ways of paying, not just the burn.

    A fee that has left the wallet for a payload that turns out not to fit is
    the exact loss this check exists to prevent, and it does not become less of
    one because the season collects by transfer.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        burn_command,
        "get_subtensor",
        lambda network: pytest.fail(
            "opened a chain connection for an unsendable round"
        ),
    )
    monkeypatch.setattr(
        burn_command,
        "execute_transfer",
        lambda **k: pytest.fail("paid for a round whose payload does not encode"),
    )

    assert (
        burn_command.perform_transfer(
            _real_season_settings(), 22, {}, verdict=_real_verdict()
        )
        is False
    )


# ─── who is allowed to pay for this hotkey ───────────────────
#
# The backend does not take the miner's word for who paid: it reads the signer
# off the chain and compares it against the chain's own owner of the announced
# hotkey. A mismatch is `fee_payer_not_owner` -- `rejected`, final, and the fee
# has already left. On the real track that is 2 TAO gone for a submission that
# was never entered, and the same comparison is made for a burn, where the money
# is not merely spent but destroyed.


class _OtherOwner(_FakeSubtensor):
    """A chain on which some other coldkey owns this workspace's hotkey."""

    def get_hotkey_owner(self, hotkey_ss58: str) -> str:
        return FEE_COLDKEY


class _NoOwner(_FakeSubtensor):
    """A chain that has no owner for this hotkey at all.

    🔴 The SDK returns `None`, and "no answer" must count as a mismatch: read
    the other way, anyone could pay a fee against anyone else's hotkey. The
    backend reads it exactly this way (`hotkey_owner` empty is a rejection).
    """

    def get_hotkey_owner(self, hotkey_ss58: str) -> None:
        return None


@pytest.mark.parametrize("chain", [_OtherOwner, _NoOwner])
def test_a_wallet_that_does_not_own_the_hotkey_pays_nothing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    chain: type[_FakeSubtensor],
) -> None:
    """🔴 The most expensive of the checks that are still free at this point."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(burn_command, "get_subtensor", lambda network: chain())
    monkeypatch.setattr(burn_command, "open_wallet", lambda cfg: _FakeWallet())
    monkeypatch.setattr(
        burn_command,
        "execute_transfer",
        lambda **k: pytest.fail("2 TAO sent for a submission the subnet will reject"),
    )
    state = _uploaded_state()
    state["competition_id"] = 3
    state["model_hash"] = "9" * 64
    save_state(30, state)

    assert (
        burn_command.perform_transfer(
            _real_season_settings(), 30, state, verdict=_real_verdict()
        )
        is False
    )
    printed = capsys.readouterr().err
    assert "Nothing was paid" in printed
    assert HOTKEY in printed  # which hotkey
    assert OWNER_COLDKEY in printed  # and who was about to pay for it
    # the checkpoint records no payment, so the round can be redone once fixed
    assert "burn_tx_hash" not in load_state(30)


def test_a_burn_is_guarded_by_the_same_ownership_check(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`judge_burn` runs the same lookup, and a burn is *more* irrecoverable
    than a transfer: there is no recipient to ask."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(burn_command, "get_subtensor", lambda network: _OtherOwner())
    monkeypatch.setattr(burn_command, "open_wallet", lambda cfg: _FakeWallet())
    monkeypatch.setattr(
        burn_command,
        "execute_stake_burn",
        lambda **k: pytest.fail("burned TAO for a submission the subnet will reject"),
    )
    save_state(31, _uploaded_state())

    assert (
        burn_command.perform_burn(
            _season_settings(), 31, _uploaded_state(), verdict=_verdict()
        )
        is False
    )


def test_an_unreadable_coldkey_is_refused_rather_than_guessed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`wallet.coldkeypub` reads a file, and raises when the file is not there.

    An address that cannot be read cannot be compared, and this is not the gate
    to carry on past: `doctor` crashed on exactly this attribute once, which is
    how we know it raises rather than returning None.
    """
    monkeypatch.chdir(tmp_path)

    class _NoColdkeypub:
        class hotkey:
            ss58_address = HOTKEY

        @property
        def coldkeypub(self) -> Any:
            raise RuntimeError("keyfile at /wallet/coldkeypub.txt does not exist")

    monkeypatch.setattr(burn_command, "get_subtensor", lambda network: _FakeSubtensor())
    monkeypatch.setattr(burn_command, "open_wallet", lambda cfg: _NoColdkeypub())
    monkeypatch.setattr(
        burn_command,
        "execute_transfer",
        lambda **k: pytest.fail("paid from a wallet we could not identify"),
    )
    state = _uploaded_state()
    state["competition_id"] = 3
    state["model_hash"] = "9" * 64
    save_state(32, state)

    assert (
        burn_command.perform_transfer(
            _real_season_settings(), 32, state, verdict=_real_verdict()
        )
        is False
    )
    assert "Nothing was paid" in capsys.readouterr().err


def test_neither_burn_nor_submit_opens_control_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """🔴 The executable half of "the payment path does not read control.json".

    ⚠️ The name still says "neither burn nor submit" because that is the claim;
    `burn` stopped being a command in 1.0, so `submit` is now the only way to
    reach the payment at all -- which makes the claim narrower to state and
    exactly as strong.

    Blocking the connection itself catches the whole family at once -- a
    re-import of `fetch_control`, an HTTP call added later, a helper that reaches
    for the rate "just to compare". The block goes on
    `urllib.request.urlopen` rather than on `openroboto.http_client.urlopen`
    because four modules bind that name into their own globals at import time,
    so a patch aimed one layer up would leave every one of them free to call out.

    ⚠️ **Not vacuous.** The workspace below really does carry a control.json URL
    -- the environment preset fills one in and it has to keep working for
    external validators -- so "no request" cannot be an artefact of there being
    nothing to request. And the run really does reach the chain: the amount that
    arrives at `execute_stake_burn` is asserted, so this cannot pass by refusing
    early.

    The competitions endpoint is faked one level up rather than blocked: it is
    the one request this path is *supposed* to make, and leaving it ambiguous
    would make the block below unreadable.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *a, **k: pytest.fail("the payment path opened an HTTP connection"),
    )

    settings = _season_settings()
    settings.control_json_url = "https://example.invalid/control.json"
    assert settings.control_json_url  # there is a file here, and nobody opens it

    # ⚠️ This used to also assert `openroboto burn` refuses on its own. That
    #    command was removed in 1.0, so the hole it guarded is now structural:
    #    there is no entry point that pays without `submit`.

    live = Competition.model_validate(
        {
            "id": 2,
            "track": "sim",
            "seq": 2,
            "label": "LingBot-VLA 2.0",
            "adapter": "sim_lingbot",
            "status": "active",
            "params": {"fee": {"kind": "burn", "amount_tao": 0.25, "coldkey": None}},
        }
    )
    monkeypatch.setattr(
        competition_module,
        "fetch_competitions",
        lambda url: SimpleNamespace(data=[live]),
    )
    monkeypatch.setattr(competition_module, "_confirmed", lambda: True)
    monkeypatch.setattr(
        submit_command.Settings, "load", staticmethod(lambda path: settings)
    )
    monkeypatch.setattr(submit_command, "perform_upload", lambda *a, **k: None)
    monkeypatch.setattr(submit_command, "perform_announce", lambda *a, **k: True)
    monkeypatch.setattr(submit_command, "fetch_tree", lambda *a, **k: GOOD_TREE)
    # The other request this path is supposed to make, faked one level up for
    # the same reason as the competitions endpoint: leaving a legitimate call
    # inside the block below would make the block unreadable.
    monkeypatch.setattr(submit_command, "fetch_roster", lambda *a, **k: _roster())
    monkeypatch.setattr(burn_command, "get_subtensor", lambda network: _FakeSubtensor())
    monkeypatch.setattr(burn_command, "open_wallet", lambda cfg: _FakeWallet())
    burned: dict[str, Any] = {}

    def _burn(**kwargs: Any) -> Any:
        burned.update(kwargs)
        return SimpleNamespace(tx_hash="0x" + "f" * 64, block_number=1)

    monkeypatch.setattr(burn_command, "execute_stake_burn", _burn)
    save_state(21, _uploaded_state())

    args = argparse.Namespace(config="miner.yaml", round=21, output_dir="", force=False)
    assert submit_command.run(args) == 0
    assert burned["amount_tao"] == 0.25  # it really did get as far as paying


def test_a_rate_typed_into_miner_yaml_cannot_lower_what_submit_pays(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """🔴 What a miner types cannot decide what a miner pays.

    `payment.burn_rate_tao` is still a field in `miner.yaml`. Nothing on the
    payment path reads it: the amount comes from the season fetched seconds
    earlier, so setting it to a tenth of the fee changes nothing about what
    leaves the wallet.

    This is the half that survives `openroboto burn` being removed. The other
    half -- paying with an amount and *no season at all* -- is now impossible
    to express, so it is no longer tested here (see the note below).
    """
    monkeypatch.chdir(tmp_path)
    settings = _season_settings()
    settings.burn_rate_tao = 0.01  # a tenth of what the season charges
    save_state(9, _uploaded_state())
    _, paid = _submitting(monkeypatch, settings)

    args = argparse.Namespace(config="miner.yaml", round=9, output_dir="", force=False)
    assert submit_command.run(args) == 0

    assert len(paid) == 1
    assert paid[0] == 0.25, "the hand-typed rate reached the wallet"


# 🔴 **Two tests were removed here on 2026-08-28, with `openroboto burn`.**
#
#    They pinned that `burn` on its own refuses -- once because it cannot
#    obtain a verdict, once because a hand-typed `payment.burn_rate_tao`
#    supplies an amount but not a season (fee spent, commitment on chain,
#    filed under whichever season the backend defaults to, no error anywhere).
#
#    That hole is now closed by construction rather than by a guard: there is
#    no entry point that reaches a payment except `submit`, and `submit` gets
#    its amount from the live season it just confirmed. A test asserting that a
#    deleted command refuses would go green by import error.
#
#    ⚠️ The half that is still reachable is still tested:
#    `test_a_rate_typed_into_miner_yaml_cannot_lower_what_submit_pays` below.


# ─── the layout gate before the money ────────────────────────
#
# `openroboto check` is a command a miner may never type, and until this gate
# existed that was the only place the layout rules ran before the fee. Everyone
# else met them in the backend's admission, which runs *after* the payment and
# ends in `HF_STRUCTURE_INVALID` -- `rejected`, final, not refunded, and the
# model may well have been fine.
#
# Every case below asserts on **call counts** of the payment, because that is
# the only assertion that separates "refused" from "refused after paying".
#
# The season is resolved *before* this gate and the season check is therefore
# counted as one call, not zero: the rule book that judges the repository is on
# the live row, and the snapshot in `miner.yaml` is a copy of that row taken at
# `init` which the season may have moved on from since. One GET is what it costs
# to be judged here by the same book admission will use -- the alternative was
# passing here on last month's rules and being rejected there on this month's,
# after the fee.


def _refused(
    monkeypatch: pytest.MonkeyPatch, round_num: int, tree: Any, force: bool = False
) -> tuple[list[Any], list[Any], int]:
    """Run `submit` over `tree` and return (season checks, payments, exit code).

    `tree` may be a listing or an exception to raise instead of returning one.
    """
    state = _uploaded_state()
    if force:
        state["burn_tx_hash"] = "0x" + "d" * 64
    save_state(round_num, state)
    checked, paid = _submitting(monkeypatch, _season_settings())
    if isinstance(tree, Exception):
        monkeypatch.setattr(
            submit_command,
            "fetch_tree",
            lambda *a, **k: _raise(tree),
        )
    else:
        monkeypatch.setattr(submit_command, "fetch_tree", lambda *a, **k: tree)
    monkeypatch.setattr(
        submit_command,
        "perform_announce",
        lambda *a, **k: pytest.fail("announced a submission that was never paid for"),
    )

    args = argparse.Namespace(
        config="miner.yaml", round=round_num, output_dir="", force=force
    )
    return checked, paid, submit_command.run(args)


def _raise(exc: Exception) -> Any:
    raise exc


def test_a_repository_the_rules_refuse_is_never_paid_for(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The original sin, one layer earlier: a bare LoRA adapter.

    The evaluator merges nothing and there is no `openroboto merge`, so this is
    unscoreable and admission says so -- after the fee. Here it costs a command.
    """
    monkeypatch.chdir(tmp_path)
    checked, paid, code = _refused(
        monkeypatch,
        20,
        [
            {"type": "file", "path": "config.json", "size": 31},
            {"type": "file", "path": "adapter_config.json", "size": 500},
            {
                "type": "file",
                "path": "adapter_model.safetensors",
                "size": 400_000_000,
            },
        ],
    )

    assert code == 1
    assert paid == []
    # the season *was* resolved first -- that is where the rule book comes from
    # -- and the refusal still landed before the prompt and before the money
    assert len(checked) == 1
    printed = capsys.readouterr()
    assert "nothing was paid" in printed.err
    assert "bare_lora_adapter" in printed.out


def test_the_gate_judges_the_repository_not_this_round_s_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """🔴 The reason this reads the HuggingFace listing rather than the local
    checkpoint directory.

    `build_repo_id` is `{user}/pi05-{last 12 of the hotkey}` -- **one repository
    for the miner's whole career** -- and `upload_folder` never deletes. So the
    repository this fee buys a verdict on is round 7 laid on top of rounds 1 to
    6, and a `.cache/` left behind by an earlier push is `LEFTOVER_UPLOAD_STATE`
    to admission: a terminal rejection, with the fee gone.

    Everything below `.cache/` is invisible to anything that walks *this
    round's* output directory, which is exactly what makes a local-only check a
    guess at the verdict rather than the verdict.
    """
    monkeypatch.chdir(tmp_path)
    checked, paid, code = _refused(
        monkeypatch,
        21,
        [*GOOD_TREE, {"type": "file", "path": ".cache/huggingface/x", "size": 12}],
    )

    assert code == 1
    assert paid == []
    assert len(checked) == 1
    assert "leftover_upload_state" in capsys.readouterr().out


def test_a_nested_checkpoint_stops_the_run_and_names_the_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The expensive half: admission **accepts** this and the evaluator then
    finds nothing, so the fee and the queue slot are spent for no score.

    The vendor's own post-trained artifact is laid out this way, so uploading
    the training output unchanged is the normal way to arrive here -- which is
    why the refusal has to name the directory rather than say "invalid layout".
    """
    monkeypatch.chdir(tmp_path)
    deep = "checkpoints/global_step_50000/hf_ckpt"
    checked, paid, code = _refused(
        monkeypatch,
        22,
        [
            {"type": "file", "path": f"{deep}/{entry['path']}", **_size(entry)}
            for entry in GOOD_TREE
            if entry["type"] == "file"
        ],
    )

    assert code == 1
    assert paid == []
    assert len(checked) == 1
    printed = capsys.readouterr()
    assert "nested_too_deep" in printed.out
    assert deep in printed.out  # copyable, not "your structure is invalid"
    # and it says why a green admission verdict is still a stop
    assert "the evaluator cannot load it" in printed.out


def test_a_listing_hf_would_not_serve_is_not_paid_through(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """🔴 "We could not check" is not "your model is fine".

    AGENTS.md §4 (infrastructure trouble is not the user's fault) governs what
    this *says*; it does not buy a way past the fee. Paying on an answer we
    never got is today's behaviour with an apology attached, and today's
    behaviour lands on a terminal rejection after the money is gone.

    Stopping consumes nothing -- the upload stays in the checkpoint and is
    reused -- so the cost of being wrong here is one command, against a
    non-refundable fee for being wrong the other way. It is also the answer the
    season gate one step later already gives when the backend is unreachable.
    """
    monkeypatch.chdir(tmp_path)
    checked, paid, code = _refused(
        monkeypatch, 23, submit_command.TreeError("HuggingFace returned HTTP 503")
    )

    assert code == 1
    assert paid == []
    assert len(checked) == 1
    printed = capsys.readouterr()
    assert "nothing was paid" in printed.err
    # named as infrastructure, not as a verdict on the model
    assert "not a verdict on your model" in printed.err
    assert "HTTP 503" in printed.err
    # no rule ever ran, so no issue code may be printed as though one had
    assert "missing_weights" not in printed.out


def test_force_does_not_skip_the_layout_gate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`--force` means "burn again", not "burn without looking" -- and there is
    no flag that means the second thing."""
    monkeypatch.chdir(tmp_path)
    _, paid, code = _refused(monkeypatch, 24, [], force=True)

    assert code == 1
    assert paid == []


def test_the_gate_judges_the_revision_that_goes_on_chain(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A gate that judged some other revision would be theatre.

    The checkpoint here holds a stale `hf_commit` alongside a URL carrying the
    real one, which is the state `push_model` leaves when the commit came back
    in the URL. `announce` pins the URL's commit, so this must judge that one.
    """
    monkeypatch.chdir(tmp_path)
    state = _uploaded_state()
    state["hf_url"] = f"https://huggingface.co/kyleab/pi05-abcdefghijkl/commit/{COMMIT}"
    state["hf_commit"] = "9" * 40  # stale, and not what announce would send
    save_state(25, state)
    _submitting(monkeypatch, _season_settings())

    asked: list[tuple[str, str]] = []

    def _tree(repo: str, revision: str, token: str = "") -> Any:
        asked.append((repo, revision))
        return GOOD_TREE

    monkeypatch.setattr(submit_command, "fetch_tree", _tree)

    args = argparse.Namespace(config="miner.yaml", round=25, output_dir="", force=False)
    assert submit_command.run(args) == 0
    assert asked == [("kyleab/pi05-abcdefghijkl", COMMIT)]
    assert announced_commit(load_state(25)) == COMMIT


def test_a_round_that_already_paid_is_not_stopped_by_the_gate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """🔴 After the fee, refusing costs more than allowing.

    The money is gone and the only thing that can still make it count is the
    commitment; a gate that fired here would turn "rejected for its layout" into
    "paid and not even submitted". The gate belongs strictly before the payment,
    and `--force`, which clears the burn, puts the round back in front of it.
    """
    monkeypatch.chdir(tmp_path)
    state = _uploaded_state()
    state["burn_tx_hash"] = "0x" + "d" * 64
    state["burn_block"] = 8_888_880
    save_state(26, state)
    _submitting(monkeypatch, _season_settings())
    monkeypatch.setattr(
        submit_command,
        "fetch_tree",
        lambda *a, **k: pytest.fail("judged a layout whose fee is already spent"),
    )

    args = argparse.Namespace(config="miner.yaml", round=26, output_dir="", force=False)
    assert submit_command.run(args) == 0


def _size(entry: dict[str, Any]) -> dict[str, Any]:
    return {"size": entry["size"]}


def test_a_repository_with_no_lfs_object_is_never_paid_for(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """🔴 A perfectly shaped repository holding no weights at all.

    Every path and every byte count the layout rules ask about is right here --
    the shards are listed, the index is listed -- and there is not one LFS object
    behind them: the pointers were pushed and the weights were not. The
    fingerprint of that repository is the empty string, which is a sentinel:
    admission's sixth gate files it `MODEL_HASH_FAILED`, terminal, after the fee.

    The real track already catches this in `upload`, because it has to compute
    the fingerprint anyway to put it on chain. **The simulation track never
    computes one** -- those repositories are public, so the backend does it
    itself -- so until this gate the entire simulation side met the question for
    the first time on the far side of the payment.
    """
    monkeypatch.chdir(tmp_path)
    pointers_only = [
        {key: value for key, value in entry.items() if key != "lfs"}
        for entry in GOOD_TREE
    ]
    _, paid, code = _refused(monkeypatch, 27, pointers_only)

    assert code == 1
    assert paid == []
    printed = capsys.readouterr()
    assert "no LFS file" in printed.err
    assert "nothing was paid" in printed.err.lower()
    # It is not a layout verdict and must not be dressed as one: the layout is
    # fine, which is exactly what makes this the confusing case.
    assert "missing_weights" not in printed.out


# ─── one model, one entry: the dedup slot, before the money ──
#
# The subnet counts a model once per season: the key is
# `(hotkey, competition_id, hf_commit)`. Paying a second time for the same
# commit buys a `skipped` -- nothing queued, nothing evaluated, nothing
# refunded -- and every ordinary way of getting there looks like a normal run:
# `--force` (which clears the payment and keeps the upload), a re-run after a
# crash, or a miner submitting the same checkpoint by hand.
#
# 🔴 The key is per *model*, not per season. Entering the same season again with
# a different model is normal and pays again, and none of these cases may stop
# that.


def test_a_commit_the_backend_already_has_is_not_paid_for_twice(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    save_state(28, _uploaded_state())
    _, paid = _submitting(
        monkeypatch, _season_settings(), roster=_roster(_entry(COMMIT))
    )
    monkeypatch.setattr(
        submit_command,
        "perform_announce",
        lambda *a, **k: pytest.fail("announced a submission that was never paid for"),
    )

    args = argparse.Namespace(config="miner.yaml", round=28, output_dir="", force=False)
    assert submit_command.run(args) == 1
    assert paid == []
    printed = capsys.readouterr().err
    assert "already entered" in printed
    assert "nothing was paid" in printed.lower()
    # and it says what a second entry would take, because "you already submitted"
    # reads as "so you are done" to someone holding a better checkpoint
    assert "train again" in printed


def test_force_pays_for_a_new_model_and_refuses_to_pay_twice_for_the_old_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """🔴 The hole `--force` used to be, and the reason the fix is not in the flag.

    `--force` clears the payment out of the checkpoint and keeps the upload, so
    the second run re-pays for the *same* commit -- and nothing about it looked
    wrong: the upload was skipped because it was already there, the payment went
    through, the run reported success, and the backend filed a `skipped`.
    """
    monkeypatch.chdir(tmp_path)
    state = _uploaded_state()
    state.update({"step": "announce", "burn_tx_hash": "0x" + "d" * 64})
    save_state(29, state)
    _, paid = _submitting(
        monkeypatch, _season_settings(), roster=_roster(_entry(COMMIT))
    )

    args = argparse.Namespace(config="miner.yaml", round=29, output_dir="", force=True)
    assert submit_command.run(args) == 1
    assert paid == []


def test_a_rejected_entry_does_not_block_the_same_model_from_being_fixed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """🔴 A rejection for a real reason releases the slot, and that is the design.

    `counts_as_submitted` is the backend's own conclusion for exactly this
    reason: "was pushed aside by a later submission" and "was really rejected"
    are the same word in the `status` column, and the difference decides whether
    the miner may pay again. Copying the rule over here would get it wrong in
    the direction that spends.
    """
    monkeypatch.chdir(tmp_path)
    save_state(33, _uploaded_state())
    _, paid = _submitting(
        monkeypatch,
        _season_settings(),
        roster=_roster(_entry(COMMIT, counts_as_submitted=False)),
    )

    args = argparse.Namespace(config="miner.yaml", round=33, output_dir="", force=False)
    assert submit_command.run(args) == 0
    assert paid == [0.25]


def test_another_model_in_the_same_season_is_a_normal_second_entry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The dedup key is per model. A season is not entered once."""
    monkeypatch.chdir(tmp_path)
    save_state(34, _uploaded_state())
    _, paid = _submitting(
        monkeypatch, _season_settings(), roster=_roster(_entry("b" * 40))
    )

    args = argparse.Namespace(config="miner.yaml", round=34, output_dir="", force=False)
    assert submit_command.run(args) == 0
    assert paid == [0.25]


def test_a_backend_that_cannot_answer_the_dedup_question_is_not_paid_through(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The same answer this path gives everywhere else it cannot get one."""
    monkeypatch.chdir(tmp_path)
    save_state(35, _uploaded_state())
    _, paid = _submitting(monkeypatch, _season_settings())
    monkeypatch.setattr(
        submit_command,
        "fetch_roster",
        lambda *a, **k: _raise(submit_command.BackendError("connection refused")),
    )

    args = argparse.Namespace(config="miner.yaml", round=35, output_dir="", force=False)
    assert submit_command.run(args) == 1
    assert paid == []
    assert "nothing was paid" in capsys.readouterr().err.lower()


def test_the_dedup_question_is_asked_about_this_hotkey_and_this_season(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """🔴 The season id is the **resolved** one, not the number in miner.yaml.

    `id` is local to one database, and asking the wrong season's entry list
    would answer "not submitted" about a season nobody is entering.
    """
    monkeypatch.chdir(tmp_path)
    save_state(36, _uploaded_state())
    asked: list[Any] = []
    _submitting(monkeypatch, _season_settings())
    monkeypatch.setattr(
        submit_command,
        "fetch_roster",
        lambda url, cid, **kwargs: (
            asked.append((cid, kwargs.get("hotkey"))) or _roster()
        ),
    )

    args = argparse.Namespace(config="miner.yaml", round=36, output_dir="", force=False)
    assert submit_command.run(args) == 0
    assert asked == [(2, HOTKEY)]


# ─── the order of the gates, and what the prompt is for ──────


def test_the_real_track_is_not_judged_by_a_book_admission_will_not_open(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """🔴 The real track has no layout rules, so this gate must not invent one.

    Admission does not judge real-track layout on purpose: the base model is
    undecided there, and judging such a checkpoint by the openpi directory
    rules is a silent misjudgement costing the miner 2 TAO
    (backend `admit_real()`, gate 4).

    This gate ran anyway, because `layout_of` picks a rule book from
    `base_model_family` alone and `real/1` says `openpi`. It refused a
    submission the backend would have taken -- `missing_weights` against rules
    nobody will ever apply -- and since no real-track checkpoint is in openpi
    layout, that refusal blocked **the entire real track**. Found by running
    the flow end to end on testnet, not by reading it.

    ⚠️ Not a relaxation. The rule is unchanged: judge by the book admission
    will use. For this track that book is empty, and an empty book acquits --
    so the payment must be reached.
    """
    monkeypatch.chdir(tmp_path)
    save_state(1, _uploaded_state())
    # A LingBot listing: nothing in it satisfies the openpi rules, which is
    # exactly the shape that used to be refused here.
    _, paid = _submitting(monkeypatch, _real_season_settings())
    monkeypatch.setattr(
        submit_command,
        "resolve_competition",
        lambda *a, **k: SimpleNamespace(
            live=_live_row(
                id=3,
                track="real",
                seq=1,
                adapter="real_xarm6",
                base_model_family="openpi",
            ),
            kind="transfer",
            amount_tao=2.0,
            cid=3,
        ),
    )

    args = argparse.Namespace(config="miner.yaml", round=1, output_dir="", force=False)
    assert submit_command.run(args) == 0
    assert len(paid) == 1, "the real track never reached the payment"
    assert "missing_weights" not in capsys.readouterr().out


def test_the_rule_book_comes_from_the_live_row_not_from_miner_yaml(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """🔴 A season that changed its base model after `init`.

    The workspace was set up for LingBot and the repository is a LingBot one;
    the season now says `openpi`, and admission will judge it by the π0.5 rules
    -- after the fee. Judging it here by the snapshot's rules means printing
    "layout ok" and paying for a submission the backend has already decided
    against, which is this gate promising something it did not do.
    """
    monkeypatch.chdir(tmp_path)
    save_state(37, _uploaded_state())
    _, paid = _submitting(monkeypatch, _season_settings())
    monkeypatch.setattr(
        submit_command,
        "resolve_competition",
        lambda *a, **k: SimpleNamespace(
            live=_live_row(base_model_family="openpi"),
            kind="burn",
            amount_tao=0.25,
            cid=2,
        ),
    )

    args = argparse.Namespace(config="miner.yaml", round=37, output_dir="", force=False)
    assert submit_command.run(args) == 1
    assert paid == []
    printed = capsys.readouterr()
    assert "missing_weights" in printed.out  # judged by π0.5, as admission will
    assert "π0.5" in printed.out  # and it says which book, in the refusal


def test_nothing_is_confirmed_that_a_later_gate_would_have_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """🔴 The prompt is the **last** thing before the money, not the first.

    A miner asked to confirm a payment that is then refused anyway learns to
    answer the prompt without reading it -- and that prompt is the only place
    the season, the amount and the recipient are ever shown. So every gate that
    can still refuse runs in front of it, which is why the season check is in
    two halves at all.
    """
    monkeypatch.chdir(tmp_path)
    order: list[str] = []
    save_state(38, _uploaded_state())
    _submitting(monkeypatch, _season_settings(), roster=_roster(_entry(COMMIT)))
    monkeypatch.setattr(
        submit_command,
        "confirm_payment",
        lambda verdict: order.append("asked"),
    )
    monkeypatch.setattr(
        submit_command,
        "fetch_roster",
        lambda *a, **k: order.append("dedup") or _roster(_entry(COMMIT)),
    )
    monkeypatch.setattr(
        submit_command,
        "fetch_tree",
        lambda *a, **k: order.append("layout") or GOOD_TREE,
    )

    args = argparse.Namespace(config="miner.yaml", round=38, output_dir="", force=False)
    assert submit_command.run(args) == 1
    assert order == ["layout", "dedup"]  # and never "asked"


# ─── end to end: an unfit workspace, and the money still there ───


def test_an_unfit_workspace_costs_nothing_end_to_end(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """🔴 The whole command, from `openroboto submit` to the exit code.

    Nothing about the gate is stubbed: a real `miner.yaml`, a real workspace, a
    real `fetch_tree` talking to a fake `urlopen`. What is replaced is only what
    would leave the machine or spend money -- and every one of those is replaced
    with a call that fails the test, because "did not pay" is the assertion.

    Reading the code is not enough for this one: the gate sits behind an upload,
    a season lookup and two commands' worth of dispatch, and each of those has
    been the thing that quietly skipped a check before.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / "miner.yaml").write_text(
        json.dumps(
            {
                "subnet": {"netuid": 80, "network": "finney", "hotkey_ss58": HOTKEY},
                "huggingface": {"username": "kyleab", "token": "hf_not_a_real_token"},
                "competition": {
                    "id": 2,
                    "track": "sim",
                    "seq": 2,
                    "label": "LingBot-VLA 2.0",
                    "adapter": "sim_lingbot",
                    "params": {
                        "fee": {"kind": "burn", "amount_tao": 0.25, "coldkey": None}
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    checkpoint = tmp_path / "checkpoint"
    checkpoint.mkdir()
    (checkpoint / "adapter_model.safetensors").write_bytes(b"\0" * 32)

    # Everything that costs money or leaves the machine. Each one fails the test
    # rather than returning a plausible value: a fake that quietly succeeds
    # cannot tell "was not called" from "was called and worked".
    monkeypatch.setattr(
        upload_module,
        "push_model",
        lambda **kwargs: UploadResult(
            f"https://huggingface.co/kyleab/pi05-{HOTKEY[-12:]}/commit/{COMMIT}",
            COMMIT,
        ),
    )
    # The season really is resolved on this path -- the layout gate needs its
    # rule book -- so it is served rather than forbidden. Everything after it
    # is forbidden, which is the assertion.
    monkeypatch.setattr(
        competition_module,
        "fetch_competitions",
        lambda url, **kwargs: SimpleNamespace(data=[_live_row()]),
    )
    monkeypatch.setattr(competition_module, "_confirmed", lambda: True)
    for module, name in (
        (submit_command, "fetch_roster"),
        (burn_command, "get_subtensor"),
        (burn_command, "open_wallet"),
        (burn_command, "execute_stake_burn"),
        (announce_command, "get_subtensor"),
        (announce_command, "open_wallet"),
        (announce_command, "submit_announcement"),
    ):
        monkeypatch.setattr(
            module,
            name,
            _forbid(f"{module.__name__}.{name}"),
        )

    served: list[str] = []

    def _urlopen(request: Any, timeout: float) -> Any:
        served.append(request.full_url)
        return _FakeResponse(
            json.dumps(
                [
                    {"type": "file", "path": "adapter_model.safetensors", "size": 32},
                    {"type": "file", "path": "adapter_config.json", "size": 12},
                ]
            ).encode()
        )

    monkeypatch.setattr(tree_module, "urlopen", _urlopen)

    exit_code = main(
        [
            "submit",
            "--round",
            "3",
            "--config",
            "miner.yaml",
            "--output-dir",
            "checkpoint",
        ]
    )

    assert exit_code != 0
    # the listing really was fetched, at the commit that would have gone on chain
    assert served == [
        f"https://huggingface.co/api/models/kyleab/pi05-{HOTKEY[-12:]}"
        f"/tree/{COMMIT}?recursive=true"
    ]
    # nothing on chain, nothing paid, and the checkpoint records no payment
    state = load_state(3)
    assert "burn_tx_hash" not in state
    assert state.get("step") == "upload"
    printed = capsys.readouterr()
    assert "nothing was paid and nothing was sent on chain" in printed.err
    assert "bare_lora_adapter" in printed.out


def _forbid(what: str) -> Any:
    def _boom(*args: Any, **kwargs: Any) -> Any:
        pytest.fail(f"{what} was called; the layout was refused before the fee")

    return _boom


class _FakeResponse(io.BytesIO):
    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


# ─── the two keys 0.7.0 adds: cid and m ──────────────────────
#
# Everything a season can be looked up by stays **off** the payload: the track,
# the base model, the fee, the format rules are all columns of the row `cid`
# points at, and a second copy on chain is a second thing that can disagree with
# the database. `m` is the one exception, and only because it cannot be looked
# up -- the real track allows private repositories, so the backend cannot pull
# the weights and fingerprint them itself.

MODEL_HASH = "9" * 64
CID = 3


def _competition_settings(track: str = "real") -> Settings:
    """A `miner.yaml` that `openroboto init` wrote for one season."""
    return Settings.from_mapping(
        {
            "subnet": {"netuid": 80, "network": "finney", "hotkey_ss58": HOTKEY},
            "competition": {
                "track": track,
                "seq": 1,
                "adapter": "real_xarm6" if track == "real" else "sim_openpi",
                "params": {"fee": {"kind": "burn", "amount_tao": 0.1}},
            },
        }
    )


def _paid_state(**extra: Any) -> dict[str, Any]:
    state = _uploaded_state()
    state.update({"burn_tx_hash": "0x" + "d" * 64, "burn_block": 8_888_880})
    state.update(extra)
    return state


def test_a_real_track_payload_carries_exactly_the_nine_keys(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The full key set, asserted as a **set** rather than by searching the
    bytes for names -- a substring check passes on a payload that also carries
    three keys nobody meant to send."""
    monkeypatch.chdir(tmp_path)
    captured = _capture_announcement(monkeypatch)

    state = _paid_state(competition_id=CID, model_hash=MODEL_HASH)
    assert announce_command.perform_announce(_competition_settings(), 1, state) is True

    raw = encode(captured[0])
    assert set(json.loads(raw)) == {"s", "h", "c", "r", "i", "b", "bb", "cid", "m"}
    decoded = decode(raw).payload
    assert decoded.competition_id == CID
    assert decoded.model_hash == MODEL_HASH


def test_a_simulation_season_carries_cid_but_no_fingerprint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A public repository is one the backend can fingerprint itself, so `m` is
    not written at all -- and "not written" is a missing key, not an empty
    one."""
    monkeypatch.chdir(tmp_path)
    captured = _capture_announcement(monkeypatch)

    state = _paid_state(competition_id=CID)
    settings = _competition_settings(track="sim")
    assert announce_command.perform_announce(settings, 1, state) is True

    raw = encode(captured[0])
    assert set(json.loads(raw)) == {"s", "h", "c", "r", "i", "b", "bb", "cid"}
    assert b'"m"' not in raw


@pytest.mark.parametrize("track", ["sim", "real"])
def test_no_payload_ever_carries_track_or_a_second_pair_of_payment_keys(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, track: str
) -> None:
    """🔴 No `t`, no `f`, no `fb`.

    The track is a column on the row `cid` points at; writing it on chain as
    well creates a second source that can contradict the database. And `b`/`bb`
    are the payment credential whatever the payment was -- a separate pair for
    transfers would mean every reader had to know which pair to look at.
    """
    monkeypatch.chdir(tmp_path)
    captured = _capture_announcement(monkeypatch)

    state = _paid_state(competition_id=CID, model_hash=MODEL_HASH)
    announce_command.perform_announce(_competition_settings(track), 1, state)

    assert set(json.loads(encode(captured[0]))) & {"t", "f", "fb"} == set()


def test_the_payment_credential_keys_are_the_historical_ones(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`b` / `bb` carry whichever payment this season takes. Same keys, same
    meaning: which transaction, which block."""
    monkeypatch.chdir(tmp_path)
    captured = _capture_announcement(monkeypatch)

    state = _paid_state(competition_id=CID, model_hash=MODEL_HASH)
    announce_command.perform_announce(_competition_settings(), 1, state)

    decoded = decode(encode(captured[0])).payload
    assert decoded.burn_tx_hash == "0x" + "d" * 64
    assert decoded.burn_block == 8_888_880


def test_a_real_track_announcement_without_a_season_is_not_sent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Without `cid` the backend reads the submission as `(sim, seq=round_num)`
    -- the entry fee would buy a place on the wrong leaderboard. Refuse, and say
    so without claiming anything was sent."""
    monkeypatch.chdir(tmp_path)
    captured = _capture_announcement(monkeypatch)

    state = _paid_state(model_hash=MODEL_HASH)
    assert announce_command.perform_announce(_competition_settings(), 1, state) is False

    assert captured == []
    out = capsys.readouterr()
    assert "`cid`" in out.err
    assert "do not pay a second time" in out.err.lower()
    assert "committing on chain" not in out.out


def test_a_malformed_fingerprint_is_not_sent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """63 hex digits, or upper case: the evaluator compares strings, so a
    fingerprint that is nearly right is a fingerprint that never matches."""
    monkeypatch.chdir(tmp_path)
    captured = _capture_announcement(monkeypatch)

    state = _paid_state(competition_id=CID, model_hash="9" * 63)
    assert announce_command.perform_announce(_competition_settings(), 1, state) is False
    assert captured == []
    assert "`m`" in capsys.readouterr().err


def test_an_unknown_track_never_falls_back_to_simulation(tmp_path: Path) -> None:
    """`Track("banana")` raises and carries the offending value.

    Reading it as simulation would put a real-track submission on the simulation
    leaderboard *after* the entry fee has been paid, and nothing would look
    wrong at the time.
    """
    settings = Settings.from_mapping(
        {
            "subnet": {"netuid": 80, "network": "finney"},
            "competition": {"track": "banana", "seq": 1},
        }
    )
    with pytest.raises(ValueError) as caught:
        payload_track(settings)
    assert "banana" in str(caught.value)
    assert "sim" not in str(caught.value).replace("simulation", "")


# ─── the sentinel: None, never "" ────────────────────────────


def test_an_empty_model_hash_really_does_reach_the_chain() -> None:
    """🔴 The negative case, and it is here to stay red-adjacent on purpose.

    `encode()` decides with `is not None`, not with truthiness, so `""` is
    written out as `"m":""` -- six bytes that every older payload does not have.
    This asserts that it *would* happen, so that the assertion below (no
    production code passes `""`) is guarding something real rather than
    restating a `False`.
    """
    from openroboto.chain import build_payload

    payload = build_payload(
        hotkey_ss58=HOTKEY,
        block_hash="c" * 64,
        hf_commit=COMMIT,
        round_num=1,
        hf_repo_id="kyleab/pi05-abcdefghijkl",
        burn_tx_hash="0x" + "d" * 64,
        burn_block=8_888_880,
        model_hash="",
    )
    assert b'"m":""' in encode(payload)


def test_the_checkpoint_readers_turn_an_empty_value_into_none() -> None:
    """Which is why nothing in this repo ever hands `""` over: the two readers
    every command goes through map absent and empty onto the same `None`."""
    from openroboto.round_state import model_hash

    assert model_hash({}) is None
    assert model_hash({"model_hash": ""}) is None
    assert model_hash({"model_hash": MODEL_HASH}) == MODEL_HASH

    assert competition_id({}) is None
    assert competition_id({"competition_id": CID}) == CID


# ─── size: the new keys are paid for before the fee, not after ───


def test_the_size_estimate_counts_the_new_keys() -> None:
    """`payload_size` is what the pre-spend self-check prints and judges. If it
    did not count `cid` and `m`, a repo name near the limit would pass the
    check, the fee would be paid, and `encode()` would then refuse the
    commitment that the fee had already bought."""
    legacy = payload_size(_paid_state(), 1)
    real = payload_size(_paid_state(competition_id=CID, model_hash=MODEL_HASH), 1)

    assert real > legacy
    assert real - legacy == len(f',"cid":{CID},"m":"{MODEL_HASH}"')


def test_the_worst_real_track_payload_still_fits_on_chain() -> None:
    """368 bytes of fixed overhead leaves 144 characters for the repo name.
    Right at that limit it must encode, because the miner has paid by then."""
    state = _paid_state(
        competition_id=999_999,
        model_hash=MODEL_HASH,
        hf_repo_id="x" * 144,
        hf_commit=COMMIT,
    )
    assert payload_size(state, 999) <= 512


# ─── the gate that runs before the money ─────────────────────


@pytest.mark.parametrize(
    ("missing", "expected"),
    [
        ({}, "which season the fee is for"),
        ({"competition_id": CID}, "model fingerprint"),
    ],
    ids=["no-cid", "no-fingerprint"],
)
def test_a_real_track_burn_spends_nothing_when_a_field_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    missing: dict[str, Any],
    expected: str,
) -> None:
    """🔴 This is the one that matters. `announce` refusing afterwards saves an
    extrinsic fee; refusing **here** saves the entry fee, which is not
    refunded.

    The rule about what the real track requires is `check_payload`'s, not a
    second copy written into this repo -- two hand-written copies of it is how
    the two sides drift apart. What this repo adds is the sentence about what to
    do next, which the protocol package has no business knowing.
    """
    monkeypatch.chdir(tmp_path)

    def _explode(*args: Any, **kwargs: Any) -> None:
        raise AssertionError(
            "connected to the chain for a payload the backend would refuse -- "
            "this is exactly the path that burns TAO for nothing"
        )

    monkeypatch.setattr(burn_command, "get_subtensor", _explode)

    state = _uploaded_state()
    state.update(missing)
    # The season *was* confirmed this run -- this is the field gate behind it,
    # which does not take the caller's word for what reached the checkpoint.
    assert (
        burn_command.perform_burn(
            _competition_settings(), 1, state, verdict=_verdict(0.1, cid=CID)
        )
        is False
    )
    assert expected in capsys.readouterr().out


def test_a_simulation_burn_is_not_asked_for_the_real_track_fields(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The other side of the gate above: on the simulation track the same
    self-check passes with neither `cid` nor `m` in the checkpoint.

    Without this, "refuse when a field is missing" could be satisfied by
    demanding those fields of everyone, which would stop the seasons that do not
    have them from paying at all.
    """
    monkeypatch.chdir(tmp_path)
    from openroboto.payment import BurnReceipt

    monkeypatch.setattr(burn_command, "get_subtensor", lambda network: _FakeSubtensor())
    monkeypatch.setattr(burn_command, "open_wallet", lambda settings: _FakeWallet())
    monkeypatch.setattr(
        burn_command,
        "execute_stake_burn",
        lambda **kwargs: BurnReceipt(tx_hash="0x" + "d" * 64, block_number=8_888_880),
    )

    assert (
        burn_command.perform_burn(
            _competition_settings("sim"), 1, _uploaded_state(), _verdict(0.1)
        )
        is True
    )


# ─── model_hash reaches the checkpoint from HF, after the push ───


def _upload_settings(track: str) -> Settings:
    settings = _competition_settings(track)
    settings.hf_token = "hf_test_token"
    settings.hf_username = "kyleab"
    return settings


def _fake_push(monkeypatch: pytest.MonkeyPatch) -> None:
    from openroboto.commands import upload as upload_command
    from openroboto.huggingface import UploadResult

    monkeypatch.setattr(
        upload_command,
        "push_model",
        lambda **kwargs: UploadResult(
            url=f"https://huggingface.co/kyleab/pi05-x/commit/{COMMIT}",
            commit_sha=COMMIT,
        ),
    )


def test_the_fingerprint_is_taken_from_hf_after_the_push(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The input is the HF tree of the commit that was just created, **not** the
    local directory: LFS object hashes only exist once HF has received the
    files."""
    monkeypatch.chdir(tmp_path)
    from openroboto.commands import upload as upload_command

    _fake_push(monkeypatch)
    asked: list[tuple[str, str, str]] = []

    def _fetch(repo_id: str, revision: str, hf_token: str = "") -> str:
        asked.append((repo_id, revision, hf_token))
        return MODEL_HASH

    monkeypatch.setattr(upload_command, "fetch_model_hash", _fetch)

    state: dict[str, Any] = {"hotkey_ss58": HOTKEY}
    upload_command.perform_upload(_upload_settings("real"), 1, str(tmp_path), state)

    assert state["model_hash"] == MODEL_HASH
    assert asked == [(state["hf_repo_id"], COMMIT, "hf_test_token")]


def test_a_simulation_upload_asks_hf_for_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A public repository is one the backend fingerprints itself, so this path
    keeps working exactly as it did -- no extra request, no extra way to
    fail."""
    monkeypatch.chdir(tmp_path)
    from openroboto.commands import upload as upload_command

    _fake_push(monkeypatch)

    def _explode(*args: Any, **kwargs: Any) -> str:
        raise AssertionError("the simulation track must not need a fingerprint")

    monkeypatch.setattr(upload_command, "fetch_model_hash", _explode)

    state: dict[str, Any] = {"hotkey_ss58": HOTKEY}
    upload_command.perform_upload(_upload_settings("sim"), 1, str(tmp_path), state)
    assert "model_hash" not in state


def test_an_empty_fingerprint_stops_the_run_before_any_payment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No LFS object in the repository means the weights never arrived -- only
    the pointers did. Saying so here, in those words, beats `check_payload`
    saying "not 64 hex characters" three steps later."""
    monkeypatch.chdir(tmp_path)
    from openroboto.commands import upload as upload_command
    from openroboto.huggingface import UploadError

    _fake_push(monkeypatch)
    monkeypatch.setattr(upload_command, "fetch_model_hash", lambda *a, **k: "")

    state: dict[str, Any] = {"hotkey_ss58": HOTKEY}
    with pytest.raises(UploadError) as caught:
        upload_command.perform_upload(_upload_settings("real"), 1, str(tmp_path), state)

    assert "no LFS file" in str(caught.value)
    assert "model_hash" not in state
