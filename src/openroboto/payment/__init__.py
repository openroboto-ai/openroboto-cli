"""Payment: initiating the burn. Verification rules belong to the backend /
evaluator and are not here.
"""

from __future__ import annotations

from openroboto.payment.burn import BurnError, BurnReceipt, execute_stake_burn

__all__ = ["BurnError", "BurnReceipt", "execute_stake_burn"]
