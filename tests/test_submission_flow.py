"""Submission pipeline: upload -> burn -> announce.

Every step on this path either costs money or is irreversible, so what is tested is
**ordering and preconditions**: not one cent is burned if preflight fails; nothing is
burned twice; the commit carried in the announcement must not be empty.
Chain and HF are entirely faked, so these tests need no network, wallet or GPU.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import pytest
from openroboto_protocol.commitment import CommitmentPayload, decode, encode

from openroboto.chain.commitment import SubmitResult
from openroboto.commands import announce as announce_command
from openroboto.commands import burn as burn_command
from openroboto.commands import submit as submit_command
from openroboto.config import Settings
from openroboto.round_state import save_state

HOTKEY = "5" + "M" * 47
COMMIT = "a" * 40


def _settings() -> Settings:
    return Settings.from_mapping(
        {
            "subnet": {"netuid": 80, "network": "finney", "hotkey_ss58": HOTKEY},
            "payment": {"burn_rate_tao": 0.1},
        }
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

    def close(self) -> None:
        self.closed = True


class _FakeWallet:
    class hotkey:
        ss58_address = HOTKEY


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
    monkeypatch.setattr(burn_command, "refresh_burn_rate", lambda *a: None)

    assert burn_command.perform_burn(_settings(), 1, {}) is False


def test_burn_records_tx_and_block_in_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    from openroboto.payment import BurnReceipt

    subtensor = _FakeSubtensor()
    monkeypatch.setattr(burn_command, "get_subtensor", lambda network: subtensor)
    monkeypatch.setattr(burn_command, "open_wallet", lambda settings: _FakeWallet())
    monkeypatch.setattr(burn_command, "refresh_burn_rate", lambda *a: None)

    seen: dict[str, Any] = {}

    def _burn(**kwargs: Any) -> BurnReceipt:
        seen.update(kwargs)
        return BurnReceipt(tx_hash="0x" + "d" * 64, block_number=8_888_880)

    monkeypatch.setattr(burn_command, "execute_stake_burn", _burn)

    state = _uploaded_state()
    assert burn_command.perform_burn(_settings(), 1, state) is True
    # the rate comes from the config / control.json, not from a literal in the code
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


# ─── the rate must be known, never guessed (old default 0.01 vs 0.1 in production) ──


def test_burn_refuses_when_the_rate_is_unknown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """control.json cannot be fetched -> not one cent is burned.

    The old code kept burning with the default 0.01 while the production rate is 0.1:
    the miner burns ten times too little, the backend rejects on the amount check, and
    **the TAO is not refunded**. So there must be no fallback amount here.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(burn_command, "refresh_burn_rate", lambda *a: None)

    def _explode(*args: Any, **kwargs: Any) -> None:
        raise AssertionError(
            "connected to the chain with an unknown rate -- this is exactly the path "
            "that burns TAO for nothing"
        )

    monkeypatch.setattr(burn_command, "get_subtensor", _explode)

    settings = Settings.from_mapping({"subnet": {"netuid": 80, "network": "finney"}})
    assert settings.burn_rate_tao is None  # the default must not be any concrete amount

    assert burn_command.perform_burn(settings, 1, _uploaded_state()) is False
    assert "Could not get" in capsys.readouterr().err


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
    err = capsys.readouterr().err
    assert "51" in err and "50" in err  # distance and window; both numbers must be said


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

    monkeypatch.setattr(
        submit_command.Settings, "load", staticmethod(lambda path: _settings())
    )
    monkeypatch.setattr(submit_command, "perform_upload", lambda *a, **k: None)
    burned: list[bool] = []

    def _burn(settings: Settings, round_num: int, state: dict[str, Any]) -> bool:
        burned.append(True)
        state["burn_tx_hash"] = "0x" + "e" * 64
        return True

    monkeypatch.setattr(submit_command, "perform_burn", _burn)
    monkeypatch.setattr(submit_command, "perform_announce", lambda *a, **k: True)

    args = argparse.Namespace(config="miner.yaml", round=3, output_dir="", force=True)
    assert submit_command.run(args) == 0
    assert burned == [True]
