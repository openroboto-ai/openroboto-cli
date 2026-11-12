"""Payment: sending the entry fee. Verification rules belong to the backend /
evaluator and are not here.

Payment follows the **season's** data
(`params.fee.kind`), never something derived from the adapter or the track:

    burn      simulation      `add_stake_burn`, no recipient
    transfer  real            `Balances.transfer_keep_alive` into `fee.coldkey`
    gamma     task            `spend_gamma` from the miner-owned deposit
"""

from __future__ import annotations

from openroboto.payment.burn import BurnError, BurnReceipt, execute_stake_burn
from openroboto.payment.transfer import TransferError, execute_transfer

__all__ = [
    "BurnError",
    "BurnReceipt",
    "TransferError",
    "execute_stake_burn",
    "execute_transfer",
]
