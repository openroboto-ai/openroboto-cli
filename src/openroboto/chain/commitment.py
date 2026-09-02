"""Write a submission announcement onto the chain.

**The payload bytes are produced by `openroboto_protocol.commitment.encode()`; this
repo must not write a second implementation.** The backend decodes with the same
module when it scans the chain; if each side writes its own JSON assembly, the
result is a miner who burned TAO that nobody sees.

Submission goes through `publish_metadata_extrinsic(data_type="BigRaw")`. The
difference between `BigRaw` and `RawN` is only the **byte length** (`RawN` for
≤128), not the client version — see the module docstring of `commitment.py` in the
protocol package, which records the 2026-08 misdiagnosis that "Raw119 means an old
client".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from openroboto_protocol.commitment import CommitmentPayload, encode

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SubmitResult:
    """The result of one commitment submission."""

    ok: bool
    extrinsic_hash: str
    block_height: int
    extrinsic_index: int
    fee_tao: float
    #: The chain **actually** gave back the block containing this extrinsic.
    #:
    #: Not the same thing as `ok`: `ok` only says the SDK considers the submission
    #: successful, while `confirmed` says we got a receipt and know which block it
    #: landed in. They are kept separate because "thinking the announcement is on
    #: chain when it is not" is one of the main paths to a miner burning TAO for
    #: nothing.
    confirmed: bool = False

    @property
    def extrinsic_ref(self) -> str:
        """A `block-index` style reference; the easiest thing to paste in a bug report.

        **No block number unless it is confirmed.** This used to fall back to
        `get_current_block()`, so an unconfirmed submission would still print a
        perfectly real-looking `6123456-0` — the miner would conclude from it that
        the announcement was on chain, when it might never have made it into a
        block at all.
        """
        if not self.confirmed or not self.block_height:
            return "unconfirmed"
        return f"{self.block_height}-{self.extrinsic_index}"


def build_payload(
    *,
    hotkey_ss58: str,
    block_hash: str,
    hf_commit: str,
    competition_seq: int,
    hf_repo_id: str,
    burn_tx_hash: str,
    burn_block: int,
    competition_id: int | None = None,
    model_hash: str | None = None,
) -> CommitmentPayload:
    """Assemble the on-chain payload.

    The mapping between field meanings and on-chain key names is defined in the
    protocol package.

    🔴 **The absent value for the last two is `None`, never `""` or `0`.**
    `encode()` decides with `if payload.competition_id is not None` — a value
    test, not a truth test — so an empty string writes `"m":""` onto the chain
    and every miner's payload is six bytes longer than it was. The protocol
    package keeps those two apart deliberately: an empty string means the miner
    supplied a value that cannot be used, and `check_payload` rejects it.

    Both are passed through **verbatim**, and that is the choice: normalizing
    `""` to `None` here would make this function quietly correct its callers,
    and the caller that needed correcting is a caller that is about to write a
    fingerprint it does not have onto the chain. Callers hand over `None`;
    `tests/test_submission_flow.py` pins both halves.

    - `competition_id`: which season, on chain `cid`. Absent for every
      commitment written before 0.7.0, which the backend reads as
      `(sim, seq=competition_seq)`.
    - `model_hash`: the weights fingerprint, on chain `m`. Required on the real
      track, where the repository may be private and the backend therefore
      cannot compute it itself.
    """
    # 🔴 The first four are **positional on purpose.** The fourth field is the
    # season ordinal that goes on chain as `r`; the protocol package is renaming
    # it (`round_num` → `claimed_competition_seq`) without changing the wire
    # format or the field order, and a keyword here would break on that bump
    # while positional survives it.
    return CommitmentPayload(
        hotkey_ss58,
        block_hash,
        hf_commit,
        competition_seq,
        hf_repo_id=hf_repo_id,
        burn_tx_hash=burn_tx_hash,
        burn_block=burn_block,
        competition_id=competition_id,
        model_hash=model_hash,
    )


def submit_announcement(
    subtensor: Any, wallet: Any, netuid: int, payload: CommitmentPayload
) -> SubmitResult:
    """Send the payload to the chain as a commitment. **Returns only after inclusion.**

    In the old `utils/chain.py:108-109` both of these parameters were `False`, with
    no comment explaining why — so when "the TAO was already burned but the
    announcement never made it on chain", the command still printed success (the
    last row of the known-defects table in the backend `AGENTS.md` §7). The miner's
    money is not refunded, so here we would rather wait one more block (~12 s) in
    order to report a truthful conclusion.

    - `wait_for_inclusion=True`: only with a receipt do we know which block it
      landed in, and only then does `SubmitResult.confirmed` mean anything. What
      the backend's chain scanner looks at is inclusion; it **does not need
      finality**.
    - `wait_for_finalization=False`: finality means waiting another ~30 s or more,
      which adds no value for this workflow and purely lengthens the miner's wait.

    A timeout or RPC jitter is **not reported as a failure**: the extrinsic may
    still be included, and falsely reporting failure would make the miner think
    they have to start over. It returns `ok=False, confirmed=False`, and the
    command layer tells the miner to check `openroboto status` first.
    """
    from bittensor.core.extrinsics.serving import publish_metadata_extrinsic

    data = encode(payload)  # over 512 bytes raises CommitmentTooLargeError
    logger.info(
        "Committing on chain | repo=%s seq=%d size=%d bytes",
        payload.hf_repo_id,
        payload.round_num,
        len(data),
    )

    try:
        result = publish_metadata_extrinsic(
            subtensor=subtensor,
            wallet=wallet,
            netuid=netuid,
            data_type="BigRaw",
            data=data,
            wait_for_inclusion=True,
            wait_for_finalization=False,
        )
    except (TypeError, AttributeError, NameError):
        # We got it wrong ourselves (bad call signature, SDK changed its interface)
        # — in that case **nothing was sent at all**. Reporting it as "outcome
        # unknown" would send the miner off to check status and wait, while the
        # 50-block burn window drains away at the same time: a bug of ours turns
        # into a miner's TAO. Let it blow up.
        raise
    except Exception as exc:
        # Infrastructure failure (RPC dropped, wait timed out) — not the miner's
        # fault, and not proof that it failed to reach the chain.
        logger.warning(
            "Error while waiting for the commitment to be included in a block, "
            "outcome unknown: %s",
            exc,
        )
        return SubmitResult(
            ok=False,
            extrinsic_hash="",
            block_height=0,
            extrinsic_index=0,
            fee_tao=0.0,
            confirmed=False,
        )
    return parse_extrinsic_result(result)


def parse_extrinsic_result(result: Any) -> SubmitResult:
    """Parse the SDK's ExtrinsicResponse into a `SubmitResult`.

    The SDK returns different shapes across versions (some with
    `extrinsic_receipt`, some with only a bool, and two different names for the fee
    field), so each field falls back through `getattr` — the decision order in this
    part follows the old `utils/chain.py::_parse_result`.

    **One thing that was changed**: when no receipt was available, the old
    implementation filled `block_height` from `subtensor.get_current_block()`, with
    a comment saying it was "just so the log has a block number in it". But that
    value also fed `extrinsic_ref`, so an unconfirmed submission would print a
    block reference that looked entirely normal. Now, no receipt means
    `confirmed=False` and no invented block number (which is why the `subtensor`
    parameter is no longer needed).
    """
    extrinsic_hash = ""
    block_height = 0
    extrinsic_index = 0
    fee_tao = 0.0

    if result:
        raw_hash = getattr(result, "extrinsic_hash", None)
        if raw_hash is not None:
            extrinsic_hash = (
                raw_hash.hex() if isinstance(raw_hash, bytes) else str(raw_hash)
            )

        receipt = getattr(result, "extrinsic_receipt", None)
        if receipt is not None:
            if extrinsic_hash in ("", "0x"):
                receipt_hash = getattr(receipt, "extrinsic_hash", "")
                extrinsic_hash = (
                    receipt_hash.hex()
                    if isinstance(receipt_hash, bytes)
                    else str(receipt_hash)
                )
            block_height = int(getattr(receipt, "block_number", 0) or 0)
            extrinsic_index = int(getattr(receipt, "extrinsic_idx", 0) or 0)

        fee = getattr(result, "extrinsic_fee", None) or getattr(
            result, "transaction_tao_fee", None
        )
        if fee is not None:
            fee_tao = float(getattr(fee, "tao", 0.0))

    ok = bool(
        getattr(result, "is_success", False)
        or getattr(result, "success", False)
        or result is True
    )
    return SubmitResult(
        ok=ok,
        extrinsic_hash=extrinsic_hash,
        block_height=block_height,
        extrinsic_index=extrinsic_index,
        fee_tao=fee_tao,
        # A block number means the receipt really came back. Under
        # `wait_for_inclusion=True` that is exactly inclusion in a block.
        confirmed=ok and block_height > 0,
    )
