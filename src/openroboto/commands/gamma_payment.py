"""Gamma payment orchestration, host-wide deduplication and durable recovery."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import Any

from openroboto_protocol.gamma_payment import (
    gamma_amount_units,
    gamma_payment_reference,
)

from openroboto.chain import get_subtensor, open_wallet
from openroboto.commands.burn import _ready_to_spend, _record, pays_as_the_hotkeys_owner
from openroboto.competition import Verdict
from openroboto.competition_state import announced_commit, save_state, state_path
from openroboto.config import Settings
from openroboto.console import say
from openroboto.payment.gamma import (
    GAMMA_TX_LIFETIME_BLOCKS,
    GammaPaymentError,
    GammaPaymentNotSpent,
    execute_gamma_spend,
    recover_gamma_spend,
)

SHARED_PAYMENT_ROOT = Path.home() / ".cache/openroboto/gamma-payments"
"""One payment per genesis and backend submission deduplication slot."""


def _journal_path(competition_id: int) -> Path:
    return state_path(competition_id).with_suffix(".gamma.json")


def _shared_journal_path(journal: dict[str, Any]) -> Path:
    identity = journal["state"]
    slot = [
        journal["netuid"],
        identity["competition_id"],
        identity["hotkey_ss58"],
        announced_commit(identity),
    ]
    slot_hash = hashlib.sha256(
        json.dumps(slot, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    ).hexdigest()
    return SHARED_PAYMENT_ROOT / str(journal["genesis_hash"]) / (slot_hash + ".json")


@contextmanager
def _payment_lock(path: Path, message: str) -> Iterator[None]:
    import fcntl

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise GammaPaymentError(message) from exc
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def gamma_submission_lock(competition_id: int) -> AbstractContextManager[None]:
    """Serialize a workspace's complete submit, including checkpoint reads."""
    return _payment_lock(
        state_path(competition_id).with_suffix(".gamma.lock"),
        "Another Gamma submit is running in this workspace. "
        "Wait for it to finish, then retry; no second payment was sent.",
    )


def _read_journal(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("status") not in (
            "signed",
            "confirmed",
            "not_spent",
        ):
            raise ValueError("invalid payment journal")
        for field in ("tx_hash", "genesis_hash", "reference"):
            if not isinstance(value[field], str) or not re.fullmatch(
                r"0x[0-9a-f]{64}", value[field]
            ):
                raise ValueError(f"invalid {field}")
        for field in ("netuid", "start_block"):
            if type(value[field]) is not int or value[field] < 0:
                raise ValueError(f"invalid {field}")
        for field in ("network", "backend_url", "coldkey", "hotkey"):
            if not isinstance(value[field], str) or not value[field]:
                raise ValueError(f"invalid {field}")
        gamma_amount_units(value["amount_gamma"])
        identity = value["state"]
        if (
            not isinstance(identity, dict)
            or type(identity["competition_id"]) is not int
            or identity["competition_id"] <= 0
            or identity["hotkey_ss58"] != value["hotkey"]
            or not isinstance(identity["hf_repo_id"], str)
            or not identity["hf_repo_id"]
            or not re.fullmatch(r"[0-9a-f]{40}", announced_commit(identity))
        ):
            raise ValueError("invalid submission identity")
        expected = gamma_payment_reference(
            netuid=value["netuid"],
            competition_id=identity["competition_id"],
            hotkey=identity["hotkey_ss58"],
            hf_repo_id=identity["hf_repo_id"],
            hf_commit=announced_commit(identity),
        )
        if expected != value["reference"]:
            raise ValueError("journal identity differs from its payment reference")
        if value["status"] == "confirmed" and (
            type(value["block_number"]) is not int
            or not value["start_block"]
            <= value["block_number"]
            < value["start_block"] + GAMMA_TX_LIFETIME_BLOCKS
        ):
            raise ValueError("invalid confirmed payment block")
    except (KeyError, TypeError, OSError, ValueError) as exc:
        raise GammaPaymentError(
            f"Cannot read the Gamma payment journal {path}: {exc}. "
            "Do not delete it or pay again; ask the operator to reconcile "
            "its transaction."
        ) from exc
    return value


def _write_journal(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _save_journal(competition_id: int, value: dict[str, Any]) -> None:
    # The caller holds the shared payment lock. Its durable record wins over
    # workspace copies, including a crash between these two writes.
    _write_journal(_shared_journal_path(value), value)
    _write_journal(_journal_path(competition_id), value)


def _reference(settings: Settings, state: dict[str, Any]) -> str:
    return gamma_payment_reference(
        netuid=settings.netuid,
        competition_id=int(state["competition_id"]),
        hotkey=str(state["hotkey_ss58"]),
        hf_repo_id=str(state["hf_repo_id"]),
        hf_commit=announced_commit(state),
    )


def _resume_locked(
    settings: Settings,
    competition_id: int,
    state: dict[str, Any],
    journal: dict[str, Any],
    subtensor: Any,
) -> bool:
    if journal["status"] == "not_spent":
        return False
    if (
        journal["netuid"] != settings.netuid
        or journal["network"] != settings.network
        or journal["backend_url"] != settings.backend_url
    ):
        raise GammaPaymentError(
            "The Gamma journal belongs to another network, subnet or backend. "
            "Restore the original workspace source; no new payment was sent."
        )
    if subtensor.substrate.get_block_hash(0) != journal["genesis_hash"]:
        raise GammaPaymentError(
            "The current chain genesis differs from the Gamma journal. "
            "Restore the original chain; no new payment was sent."
        )
    if str(subtensor.get_hotkey_owner(journal["hotkey"])) != journal["coldkey"]:
        raise GammaPaymentError(
            "The submitting hotkey's owner differs from the Gamma payment. "
            "Ask the operator to reconcile it; no new payment was sent."
        )
    has_identity = all(
        state.get(key) for key in ("competition_id", "hotkey_ss58", "hf_repo_id")
    ) and bool(announced_commit(state))
    if has_identity and _reference(settings, state) != journal["reference"]:
        if _shared_journal_path(journal) == _shared_journal_path(
            journal | {"state": state}
        ):
            original_repo = journal["state"]["hf_repo_id"]
            raise GammaPaymentError(
                f"This submission slot already has a Gamma payment "
                f"for {original_repo}. "
                "Restore that original repository and reconcile its submission; "
                "no second payment was sent."
            )
        if journal["status"] == "signed":
            raise GammaPaymentError(
                "The workspace changed while a Gamma payment is unresolved. "
                "Restore its original repository revision before submitting another."
            )
        return False
    if journal["status"] == "confirmed":
        tx_hash, block_number = journal["tx_hash"], journal["block_number"]
    else:
        try:
            receipt = recover_gamma_spend(subtensor, journal)
        except GammaPaymentNotSpent:
            journal["status"] = "not_spent"
            _save_journal(competition_id, journal)
            say("The original Gamma transaction paid nothing. Rechecking before retry.")
            return False
        tx_hash, block_number = receipt.tx_hash, receipt.block_number
        journal.update(status="confirmed", block_number=block_number)
    _save_journal(competition_id, journal)
    if (
        state.get("step") != "announce"
        and subtensor.get_current_block() - block_number > settings.burn_block_window
    ):
        raise GammaPaymentError(
            f"Gamma payment {tx_hash} is confirmed but its announcement window "
            "expired. No new payment was sent. Ask the operator to reconcile "
            "the paid submission."
        )
    if not has_identity:
        state.update(journal["state"])
    if state.get("burn_tx_hash") != tx_hash:
        state.update(
            burn_tx_hash=tx_hash,
            burn_block=block_number,
            payment_kind="gamma",
            gamma_payment_reference=journal["reference"],
            step="burn",
            status="completed",
        )
        save_state(competition_id, state)
    say(f"✅ Reusing confirmed Gamma payment {tx_hash}; no new payment was sent")
    return True


def resume_gamma_payment(
    settings: Settings, competition_id: int, state: dict[str, Any]
) -> bool:
    """Reconcile the host-wide authoritative journal under its payment lock."""
    local = _read_journal(_journal_path(competition_id))
    if local is None:
        return False
    shared_path = _shared_journal_path(local)
    with _payment_lock(
        shared_path.with_suffix(".lock"),
        "This Gamma payment is being processed in another workspace. "
        "Wait, then retry; no second payment was sent.",
    ):
        journal = _read_journal(shared_path) or local
        subtensor = get_subtensor(settings.network)
        try:
            return _resume_locked(settings, competition_id, state, journal, subtensor)
        except GammaPaymentError:
            raise
        except Exception as exc:
            raise GammaPaymentError(
                f"Cannot reconcile the Gamma journal with the chain: {exc}. "
                "Retry with the original network and backend; no new payment was sent."
            ) from exc
        finally:
            subtensor.close()


def perform_gamma(
    settings: Settings, competition_id: int, state: dict[str, Any], verdict: Verdict
) -> bool:
    """Spend the exact season fee, or reuse another workspace's same payment."""
    if not _ready_to_spend(settings, competition_id, state, doing="spending Gamma"):
        return False
    assert verdict.fee.amount_gamma is not None
    reference = _reference(settings, state)
    subtensor = get_subtensor(settings.network)
    try:
        wallet = open_wallet(settings)
        if not pays_as_the_hotkeys_owner(subtensor, wallet, state):
            return False
        journal: dict[str, Any] = {
            "status": "signed",
            "network": settings.network,
            "backend_url": settings.backend_url,
            "genesis_hash": str(subtensor.substrate.get_block_hash(0)),
            "netuid": settings.netuid,
            "hotkey": str(state["hotkey_ss58"]),
            "coldkey": str(wallet.coldkeypub.ss58_address),
            "amount_gamma": verdict.fee.amount_gamma,
            "reference": reference,
            "state": dict(state),
        }
        shared_path = _shared_journal_path(journal)
        with _payment_lock(
            shared_path.with_suffix(".lock"),
            "This Gamma payment is being processed in another workspace. "
            "Wait, then retry; no second payment was sent.",
        ):
            previous = _read_journal(shared_path)
            if previous is not None and _resume_locked(
                settings, competition_id, state, previous, subtensor
            ):
                return True

            def before_submit(tx_hash: str, start_block: int) -> None:
                journal.update(tx_hash=tx_hash, start_block=start_block)
                _save_journal(competition_id, journal)

            try:
                receipt = execute_gamma_spend(
                    subtensor=subtensor,
                    wallet=wallet,
                    netuid=settings.netuid,
                    hotkey=str(state["hotkey_ss58"]),
                    amount_gamma=verdict.fee.amount_gamma,
                    reference=reference,
                    before_submit=before_submit,
                )
            except GammaPaymentNotSpent:
                journal["status"] = "not_spent"
                _save_journal(competition_id, journal)
                raise
            journal.update(status="confirmed", block_number=receipt.block_number)
            _save_journal(competition_id, journal)
    finally:
        subtensor.close()
    state.update(payment_kind="gamma", gamma_payment_reference=reference)
    _record(competition_id, state, receipt)
    return True
