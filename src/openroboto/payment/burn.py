"""发起 burn 付费（链上 `add_stake_burn`）。

⚠️ **红线：burn 金额换算与区块。** 下面三处逐字保留旧 `payment.py` 的写法：

1. `int(amount_tao * 1e9)` —— TAO → Rao 的换算，**不许改成 `round()` 或 Decimal**。
   后端按金额核对，差 1 Rao 就是拒绝，而拒绝不退款。
2. `limit` 缺省值 `0xFFFFFFFFFFFFFFFF`（max u64 = 接受市价）。
3. `wait_for_inclusion=True, wait_for_finalization=False` —— 必须等打包，
   因为 `burn_block` 要写进 commitment，后端拿它算 burn 与 commit 的区块差
   （生效窗口 50 个块，超了拒且不退）。

**校验**那一半（`verify_burn_on_chain`）没有搬过来：CLI 一处都不调它，
它是后端 / 评测方的活，该进 protocol 包共享。见 SCOPE.md 待定 #3。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

RAO_PER_TAO = 1e9
MAX_U64 = 0xFFFFFFFFFFFFFFFF
"""limit 传 max u64 = 接受任何市价。"""


class BurnError(Exception):
    """burn 没发出去。TAO 没动，可以直接重试。"""


@dataclass(frozen=True)
class BurnReceipt:
    """一次 burn 的凭据。两个字段都要写进 commitment。"""

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
    """在链上烧掉 `amount_tao`，返回 tx 与区块号。

    Raises:
        BurnError: 交易失败或没能确定区块号。
    """
    target_hotkey = hotkey_ss58 or wallet.hotkey.ss58_address
    logger.info(
        "🔥 发起 burn：%s TAO | netuid=%d | hotkey=%s...",
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
        raise BurnError(f"burn 交易失败：{receipt.error_message}")

    tx_hash = str(receipt.extrinsic_hash)
    block_number = resolve_burn_block(subtensor, receipt, tx_hash)
    if not block_number:
        raise BurnError(
            f"burn 已提交（tx={tx_hash[:16]}...）但确定不了区块号。\n"
            "  → 不要重复烧。用 `openroboto status` 查这笔是否已被扫链收下"
        )

    logger.info("✅ burn 已提交 | tx=%s... block=%d", tx_hash[:16], block_number)
    return BurnReceipt(tx_hash=tx_hash, block_number=block_number)


def resolve_burn_block(subtensor: Any, receipt: Any, tx_hash: str) -> int:
    """确定 burn 落在哪个区块。三条路依次试。

    SDK 即使 `wait_for_inclusion=True` 也可能不填 `block_number`；而这个值是
    commitment 里的 `bb`，后端用它算区块窗口 —— 填 0 等于自己把提交判死。
    """
    block_number = getattr(receipt, "block_number", None)
    if block_number:
        return int(block_number)

    block_hash = getattr(receipt, "block_hash", None)
    if block_hash:
        try:
            return int(subtensor.substrate.get_block_number(block_hash))
        except Exception as exc:  # SDK 版本差异，失败就走扫块
            logger.debug("get_block_number 失败：%s", exc)

    return scan_recent_blocks_for_tx(subtensor, tx_hash)


def scan_recent_blocks_for_tx(
    subtensor: Any,
    tx_hash: str,
    max_retries: int = 5,
    wait_sec: float = 2.0,
    depth: int = 15,
) -> int:
    """在最近 `depth` 个块里找这笔交易，找不到返回 0。

    交易刚提交时可能还在传播，所以带重试与退避。
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
        except Exception as exc:  # 扫块是兜底，链抖动不该让流程炸掉
            logger.debug("扫块失败（第 %d 次）：%s", attempt + 1, exc)
        if attempt < max_retries - 1:
            time.sleep(wait_sec)
    return 0
