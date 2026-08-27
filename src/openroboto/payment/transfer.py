"""Initiate the transfer payment (on-chain `Balances.transfer_keep_alive`).

The real track does not burn. Its entry fee is an **ordinary TAO transfer** into
the address the season publishes (`params.fee.coldkey`), and the two are not
interchangeable: `add_stake_burn` buys alpha and destroys it, leaving no
recipient at all, so a burn of the right amount pays nobody and the submission
stays unpaid with the TAO gone.

## Why `transfer_keep_alive` and not `transfer_allow_death`

The one that can empty the account also **reaps** it below the existential
deposit, and a reaped coldkey loses whatever else it held. A miner paying their
last 2 TAO would lose the remainder as well, for no gain to anyone. If the
balance is too low the extrinsic fails and nothing moves -- which is the
outcome to want on a payment that is not refundable.

## The three things the backend compares this against

`judge_transfer` (backend `services/ingest/verification/transfer.py`) reads the
extrinsic out of the block the commitment names and checks, in order: the block,
the destination, the amount, and that the signer is the on-chain owner of the
submitting hotkey. So:

1. **the coldkey that signs is the one that must own the hotkey.** `wallet.coldkey`
   signs, and `subnet.hotkey_ss58` is announced -- a workspace pointing at a
   hotkey owned by some *other* coldkey pays and is then rejected for
   `fee_payer_not_owner`, with the TAO gone. `doctor` cannot see this; the chain
   can.
2. **`int(amount_tao * 1e9)`** -- the same conversion as the burn, deliberately
   spelled the same way (`payment/burn.py` red line #1). The backend compares
   `>=`, so a rounding difference of one Rao downward is a rejection.
3. **the block number is not optional.** It goes on chain as `bb`, and the
   backend looks for the transaction *in that block*: a wrong number is
   `payment_tx_not_found`, final and unrefunded. `wait_for_finalization=False`
   leaves `receipt.block_number` unset on some SDK versions, which is why the
   resolution is shared with the burn (`resolve_burn_block`) rather than read
   off the receipt here.
"""

from __future__ import annotations

import logging
from typing import Any

from openroboto.payment.burn import RAO_PER_TAO, BurnReceipt, resolve_burn_block

logger = logging.getLogger(__name__)


class TransferError(Exception):
    """The transfer was never sent. The TAO has not moved, so retrying is safe.

    Separate from `BurnError` because the two say different things to a miner
    who is looking at their balance: "the burn failed" on a season that is paid
    by transfer would send them looking for stake that was never bought.
    """


def execute_transfer(
    subtensor: Any,
    wallet: Any,
    dest_coldkey: str,
    amount_tao: float,
) -> BurnReceipt:
    """Send `amount_tao` to `dest_coldkey` and return the tx and block number.

    Returns the same `BurnReceipt` the burn does, and that is the point: the two
    fields it carries are `b` and `bb` in the commitment, which the two tracks
    share (spec 10 §3.5 -- the payment proof does **not** get its own keys).
    Giving this path its own receipt type would be a second shape for one pair
    of on-chain facts.

    Raises:
        TransferError: the transaction failed, or the block number could not be
            determined.
    """
    logger.info("💸 Sending transfer: %s TAO → %s...", amount_tao, dest_coldkey[:12])

    call = subtensor.substrate.compose_call(
        call_module="Balances",
        call_function="transfer_keep_alive",
        call_params={"dest": dest_coldkey, "value": int(amount_tao * RAO_PER_TAO)},
    )
    extrinsic = subtensor.substrate.create_signed_extrinsic(
        call=call, keypair=wallet.coldkey
    )
    receipt = subtensor.substrate.submit_extrinsic(
        extrinsic, wait_for_inclusion=True, wait_for_finalization=False
    )

    if not receipt.is_success:
        raise TransferError(f"The transfer transaction failed: {receipt.error_message}")

    tx_hash = str(receipt.extrinsic_hash)
    block_number = resolve_burn_block(subtensor, receipt, tx_hash)
    if not block_number:
        raise TransferError(
            f"The transfer was submitted (tx={tx_hash[:16]}...) but its block "
            f"number could not be determined.\n"
            "  → do not pay again. Run `openroboto status` to check whether "
            "the chain scanner has picked this one up"
        )

    logger.info("✅ Transfer submitted | tx=%s... block=%d", tx_hash[:16], block_number)
    return BurnReceipt(tx_hash=tx_hash, block_number=block_number)
