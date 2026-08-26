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
from openroboto.preflight import check_announce_ready, payload_size, payload_track
from openroboto.round_state import (
    announced_commit,
    competition_id,
    load_state,
    save_state,
)

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


def _verdict(amount_tao: float = 0.25, cid: int = 2, kind: str = "burn") -> Any:
    """What `competition.precheck` hands back.

    It is the **only** evidence that the season was confirmed in this run, which
    is why `perform_burn` takes it as an argument rather than reading an amount
    off `Settings`: an amount can be typed into miner.yaml by hand, and a number
    says how much, never which competition.
    """
    return SimpleNamespace(
        live=SimpleNamespace(label="LingBot-VLA 2.0", id=cid),
        kind=kind,
        amount_tao=amount_tao,
        cid=cid,
    )


#: A LingBot repository the rules accept: two shards at the top, the config the
#: layout names, the index that lists them. Written as a HuggingFace listing
#: (`type` / `path` / `size`) rather than as files on disk, because the listing
#: is what the gate judges and what the backend judges.
GOOD_TREE: list[dict[str, Any]] = [
    {"type": "file", "path": ".gitattributes", "size": 1797},
    {"type": "directory", "path": "unused"},
    {"type": "file", "path": "config.json", "size": 31},
    {"type": "file", "path": "model.safetensors.index.json", "size": 92_000},
    {"type": "file", "path": "model-00001-of-00002.safetensors", "size": 6_000_000_000},
    {"type": "file", "path": "model-00002-of-00002.safetensors", "size": 5_000_000_000},
]


def _submitting(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    tree: list[dict[str, Any]] | None = None,
) -> tuple[list[Any], list[Any]]:
    """Wire `submit` up to fakes and return (precheck calls, payment calls).

    `tree` is the HuggingFace listing the layout gate judges; it defaults to a
    repository that passes, because these cases are about the *season* gate. The
    layout gate has its own section further down.
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

    checked: list[Any] = []
    paid: list[Any] = []

    def _precheck(cfg: Settings, snapshot: Any, now: Any) -> Any:
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

    monkeypatch.setattr(submit_command, "precheck", _precheck)
    monkeypatch.setattr(submit_command, "perform_burn", _burn)
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

    monkeypatch.setattr(submit_command, "precheck", _refuse)
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
    submission unpaid. Until transfers can be sent, this refuses."""
    monkeypatch.chdir(tmp_path)
    save_state(7, _uploaded_state())
    _, paid = _submitting(monkeypatch, _season_settings())
    monkeypatch.setattr(
        submit_command,
        "precheck",
        lambda *a, **k: SimpleNamespace(
            live=SimpleNamespace(label="xArm 6 第一届", id=3),
            kind="transfer",
            amount_tao=2.0,
            cid=3,
        ),
    )

    args = argparse.Namespace(config="miner.yaml", round=7, output_dir="", force=False)
    assert submit_command.run(args) == 1
    assert paid == []


def test_a_config_from_before_competitions_takes_the_old_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No section to check against, so the check does not run at all -- and the
    rate still comes from control.json, exactly as it did."""
    monkeypatch.chdir(tmp_path)
    save_state(8, _uploaded_state())
    checked, _ = _submitting(monkeypatch, _settings())
    monkeypatch.setattr(
        submit_command,
        "precheck",
        lambda *a, **k: pytest.fail("an old config has no competition to check"),
    )

    args = argparse.Namespace(config="miner.yaml", round=8, output_dir="", force=False)
    assert submit_command.run(args) == 0
    assert checked == []


def test_a_season_config_reads_no_control_json_when_it_burns(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Two sources for one number with no rule for which wins -- and this one is
    money. It is also one more request between "checked" and "paid"."""
    monkeypatch.chdir(tmp_path)
    settings = _season_settings()
    monkeypatch.setattr(
        burn_command,
        "refresh_burn_rate",
        lambda *a, **k: pytest.fail("control.json was read for a season's fee"),
    )
    monkeypatch.setattr(burn_command, "get_subtensor", lambda network: _FakeSubtensor())
    monkeypatch.setattr(burn_command, "open_wallet", lambda cfg: object())
    monkeypatch.setattr(
        burn_command,
        "execute_stake_burn",
        lambda **kwargs: SimpleNamespace(tx_hash="0x" + "f" * 64, block_number=1),
    )
    state = _uploaded_state()
    state["competition_id"] = 2
    save_state(9, state)

    assert burn_command.perform_burn(settings, 9, state, verdict=_verdict(0.25)) is True
    # the amount came from the verdict, i.e. from the row the backend served
    assert settings.burn_rate_tao == 0.25


def test_a_season_fee_nobody_checked_is_not_burned(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Reaching the burn with no verdict means the gate never ran. Falling back
    to control.json here would be that gate quietly becoming optional."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        burn_command,
        "execute_stake_burn",
        lambda **kwargs: pytest.fail("burned a fee that was never confirmed"),
    )

    assert burn_command.perform_burn(_season_settings(), 10, _uploaded_state()) is False


def test_a_rate_typed_into_miner_yaml_does_not_buy_a_place_in_a_season(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """🔴 End to end, the shape of the hole this gate closes.

    `miner.yaml` says you may set `payment.burn_rate_tao` by hand, and the guard
    here used to ask "is there an amount". So a hand-filled rate -- the *right*
    amount, even -- walked straight past the season check. What followed had no
    error anywhere in it: nothing wrote `competition_id`, so `announce` sent a
    payload with no `cid`, so the backend filed the submission under the season
    it defaults to -- the archived π0.5 one. Fee spent, commitment on chain,
    backend acknowledging it, wrong competition.

    Note what the preflight says below: on this season's track the payload is
    perfectly valid without a `cid`. Nothing downstream is going to catch this,
    which is why refusing here is the whole defence.
    """
    monkeypatch.chdir(tmp_path)
    settings = _season_settings()
    settings.burn_rate_tao = 0.25  # typed in by hand, and the correct amount
    monkeypatch.setattr(
        burn_command.Settings, "load", staticmethod(lambda path: settings)
    )
    monkeypatch.setattr(
        burn_command,
        "get_subtensor",
        lambda network: pytest.fail("connected to the chain to pay for no season"),
    )
    monkeypatch.setattr(
        burn_command,
        "refresh_burn_rate",
        lambda *a, **k: pytest.fail("a season's fee is not control.json's business"),
    )
    state = _uploaded_state()
    save_state(11, state)
    assert check_announce_ready(state, 11, payload_track(settings)) == []

    args = argparse.Namespace(config="miner.yaml", round=11)
    assert burn_command.run(args) == 1

    printed = capsys.readouterr()
    assert "nothing was burned" in printed.err.lower()
    assert "burn_rate_tao" in printed.err  # names the thing that did not count
    # Nothing about a season reached the checkpoint, so `announce` would have put
    # a payload with no `cid` on chain -- the wrong-season filing, in one read.
    assert competition_id(load_state(11)) is None


# ─── the layout gate before the money ────────────────────────
#
# `openroboto check` is a command a miner may never type, and until this gate
# existed that was the only place the layout rules ran before the fee. Everyone
# else met them in the backend's admission, which runs *after* the payment and
# ends in `HF_STRUCTURE_INVALID` -- `rejected`, final, not refunded, and the
# model may well have been fine.
#
# Every case below asserts on **call counts** of the payment and of the season
# check, because that is the only assertion that separates "refused" from
# "refused after paying".


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
    # the season was never even asked about: refusing costs no backend call
    assert checked == []
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
    assert checked == []
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
    assert checked == []
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
    assert checked == []
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
    for module, name in (
        (competition_module, "fetch_competitions"),
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
            "payment": {"burn_rate_tao": 0.1},
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
    from openroboto.round_state import competition_id, model_hash

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
    monkeypatch.setattr(burn_command, "refresh_burn_rate", lambda *a: None)

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


def test_a_legacy_burn_is_not_asked_for_the_new_fields(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The same self-check, on a config with no competition section, still
    passes with neither `cid` nor `m` anywhere in the checkpoint."""
    monkeypatch.chdir(tmp_path)
    from openroboto.payment import BurnReceipt

    monkeypatch.setattr(burn_command, "refresh_burn_rate", lambda *a: None)
    monkeypatch.setattr(burn_command, "get_subtensor", lambda network: _FakeSubtensor())
    monkeypatch.setattr(burn_command, "open_wallet", lambda settings: _FakeWallet())
    monkeypatch.setattr(
        burn_command,
        "execute_stake_burn",
        lambda **kwargs: BurnReceipt(tx_hash="0x" + "d" * 64, block_number=8_888_880),
    )

    assert burn_command.perform_burn(_settings(), 1, _uploaded_state()) is True


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
