"""外部验证者的权重归一化与上链。

⚠️ **红线：u16 归一化。** `normalize_weights()` 的表达式是链上排放的最后一道换算，
搬家时逐字保留旧 `validator.py` 的写法 —— 只保留正权重、先归一到 sum=1.0、
再 `int(w * 65535)` 截断（不是四舍五入）。改成 `round()` 会让每个矿工的权重
差 1 个 u16 单位，与后端算出来的期望值对不上。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

U16_MAX = 65535
"""Bittensor 权重的定点上限。"""


@dataclass(frozen=True)
class NormalizedWeights:
    """归一化结果。uids 与 weights 一一对应，顺序即上链顺序。"""

    uids: list[int]
    weights: list[int]
    detail: list[str]
    """逐条明细，直接打进日志 —— 权重设错时这几行是唯一的现场。"""


def normalize_weights(
    weights_raw: dict[str, float], hotkeys: list[str]
) -> NormalizedWeights:
    """把后端给的 `{hotkey: 权重}` 换成链要的 `(uid, u16)`。

    Args:
        weights_raw: 后端 `/api/weights` 的原始权重，键是 hotkey。
        hotkeys: metagraph 里的 hotkey 列表，下标即 uid。
    """
    positive: dict[int, float] = {}
    detail: list[str] = []
    for uid, hotkey in enumerate(hotkeys):
        weight = weights_raw.get(hotkey, 0.0)
        if weight > 0:
            positive[uid] = weight
            detail.append(f"  uid={uid:3d} hotkey={hotkey[:12]}... raw={weight:.6f}")

    if not positive:
        return NormalizedWeights([], [], ["没有正权重"])

    total = sum(positive.values())
    normed = {uid: weight / total for uid, weight in positive.items()}

    uids = list(normed.keys())
    # 红线表达式：截断，不是四舍五入。
    weights = [int(w * U16_MAX) for w in normed.values()]

    detail.append(f"  原始合计={total:.6f}，归一化到 sum=1.0")
    detail.extend(
        f"  → uid={uid:3d} u16={w:5d} ({normed[uid]:.6f})"
        for uid, w in zip(uids, weights, strict=True)
    )
    return NormalizedWeights(uids, weights, detail)


def set_weights_on_chain(
    subtensor: Any,
    wallet: Any,
    netuid: int,
    weights_raw: dict[str, float],
    hotkeys: list[str],
) -> bool:
    """归一化后设权重。全零权重不发交易（发了也只是白付手续费）。"""
    normalized = normalize_weights(weights_raw, hotkeys)
    for line in normalized.detail:
        logger.info("[set_weights]%s", line)

    if not normalized.uids:
        logger.warning("[set_weights] 没有正权重，本次不设")
        return False

    logger.info(
        "[set_weights] 设 %d 个非零权重 | netuid=%d", len(normalized.uids), netuid
    )
    result = subtensor.set_weights(
        wallet=wallet,
        netuid=netuid,
        uids=normalized.uids,
        weights=normalized.weights,
    )
    return _is_success(result)


def _is_success(result: Any) -> bool:
    """判断 set_weights 的返回是不是成功。

    SDK 有三种返回形状（旧的布尔、标准的 `is_success`、timelock 版的
    `success` + `error=None`），旧 `validator.py` 的判定顺序原样保留。
    """
    if not result:
        logger.warning("[set_weights] set_weights 返回假值")
        return False

    success = bool(
        getattr(result, "is_success", False)
        or getattr(result, "success", False)
        or result is True
        or (
            getattr(result, "error", "missing") is None
            and (
                getattr(result, "success", False)
                or str(getattr(result, "message", "") or "").lower() == "success"
            )
        )
    )
    if success:
        logger.info("[set_weights] ✅ 链上确认")
        return True

    message = getattr(result, "status_message", "") or str(result)
    logger.error("[set_weights] ❌ 链上失败 | msg=%s", message)
    return False
