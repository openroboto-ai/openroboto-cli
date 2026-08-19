"""Initiate the burn payment (on-chain `add_stake_burn`).

⚠️ **Red line: burn amount conversion and block.** The three places below preserve
the old `payment.py` formulation verbatim:

1. `int(amount_tao * 1e9)` — the TAO → Rao conversion, which **must not be changed
   to `round()` or Decimal**. The backend checks against the amount; being off by
   1 Rao means rejection, and rejection means no refund.
2. The `limit` default `0xFFFFFFFFFFFFFFFF` (max u64 = accept the market price).
3. `wait_for_inclusion=True, wait_for_finalization=False` — inclusion must be
   awaited, because `burn_block` goes into the commitment and the backend uses it
   to compute the block distance between burn and commit (the effective window is
   50 blocks; going over means rejected and not refunded).

The **verification** half (`verify_burn_on_chain`) was not moved over: the CLI
never calls it, it is the backend's / evaluator's job, and it should go into the
protocol package to be shared. See SCOPE.md open item #3.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

RAO_PER_TAO = 1e9
MAX_U64 = 0xFFFFFFFFFFFFFFFF
"""Passing max u64 as the limit = accept any market price."""


class BurnError(Exception):
    """The burn was never sent. The TAO has not moved, so retrying directly is safe."""


@dataclass(frozen=True)
class BurnReceipt:
    """Proof of one burn. Both fields go into the commitment."""

    tx_hash: str
    block_number: int


def execute_stake_burn(
    subtensor: Any,
    wallet: Any,
    netuid: int,
    amount_tao: float,
    hotkey_ss58: str | None = None,
    limit_price_rao: int = 0,
) -> BurnReceipt:
    """Burn `amount_tao` on chain and return the tx and block number.

    Raises:
        BurnError: the transaction failed, or the block number could not be
            determined.
    """
    target_hotkey = hotkey_ss58 or wallet.hotkey.ss58_address
    logger.info(
        "🔥 Sending burn: %s TAO | netuid=%d | hotkey=%s...",
        amount_tao,
        netuid,
        target_hotkey[:8],
    )

    call = subtensor.substrate.compose_call(
        call_module="SubtensorModule",
        call_function="add_stake_burn",
        call_params={
            "netuid": netuid,
            "amount": int(amount_tao * RAO_PER_TAO),
            "hotkey": target_hotkey,
            "limit": limit_price_rao if limit_price_rao > 0 else MAX_U64,
        },
    )
    extrinsic = subtensor.substrate.create_signed_extrinsic(
        call=call, keypair=wallet.coldkey
    )
    receipt = subtensor.substrate.submit_extrinsic(
        extrinsic, wait_for_inclusion=True, wait_for_finalization=False
    )

    if not receipt.is_success:
        raise BurnError(f"The burn transaction failed: {receipt.error_message}")

    tx_hash = str(receipt.extrinsic_hash)
    block_number = resolve_burn_block(subtensor, receipt, tx_hash)
    if not block_number:
        raise BurnError(
            f"The burn was submitted (tx={tx_hash[:16]}...) but its block number "
            f"could not be determined.\n"
            "  \u2192 do not burn again. Run `openroboto status` to check whether "
            "the chain scanner has picked this one up"
        )

    logger.info("✅ Burn submitted | tx=%s... block=%d", tx_hash[:16], block_number)
    return BurnReceipt(tx_hash=tx_hash, block_number=block_number)


def resolve_burn_block(subtensor: Any, receipt: Any, tx_hash: str) -> int:
    """Determine which block the burn landed in. Three routes, tried in order.

    Even with `wait_for_inclusion=True` the SDK may leave `block_number` unset; but
    this value is `bb` in the commitment, which the backend uses to compute the
    block window — filling in 0 is sentencing your own submission to death.
    """
    block_number = getattr(receipt, "block_number", None)
    if block_number:
        return int(block_number)

    block_hash = getattr(receipt, "block_hash", None)
    if block_hash:
        try:
            return int(subtensor.substrate.get_block_number(block_hash))
        except Exception as exc:  # SDK version differences; on failure, scan blocks
            logger.debug("get_block_number failed: %s", exc)

    return scan_recent_blocks_for_tx(subtensor, tx_hash)


def scan_recent_blocks_for_tx(
    subtensor: Any,
    tx_hash: str,
    max_retries: int = 5,
    wait_sec: float = 2.0,
    depth: int = 15,
) -> int:
    """Look for this transaction in the last `depth` blocks; return 0 if not found.

    A freshly submitted transaction may still be propagating, hence the retries and
    backoff.
    """
    bare_hash = tx_hash.replace("0x", "")
    for attempt in range(max_retries):
        try:
            head = subtensor.substrate.get_block()
            if head:
                head_number = int(head["header"]["number"])
                for number in range(head_number, max(0, head_number - depth), -1):
                    block = subtensor.substrate.get_block(block_number=number)
                    if not block:
                        continue
                    for extrinsic in block.get("extrinsics", []):
                        found = extrinsic.get("extrinsic_hash", "")
                        if found in (tx_hash, bare_hash, f"0x{bare_hash}"):
                            return number
        except Exception as exc:  # fallback scan; chain jitter must not break the run
            logger.debug("Block scan failed (attempt %d): %s", attempt + 1, exc)
        if attempt < max_retries - 1:
            time.sleep(wait_sec)
    return 0
