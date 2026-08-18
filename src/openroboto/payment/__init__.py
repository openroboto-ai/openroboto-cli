"""付费：发起 burn。校验规则属于后端 / 评测方，不在这里。"""

from __future__ import annotations

from openroboto.payment.burn import BurnError, BurnReceipt, execute_stake_burn

__all__ = ["BurnError", "BurnReceipt", "execute_stake_burn"]
