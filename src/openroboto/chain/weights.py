"""Weight normalization and on-chain submission for external validators.

⚠️ **Red line: u16 normalization.** The expression in `normalize_weights()` is the
last conversion step before on-chain emissions; the old `validator.py` formulation
was preserved verbatim during the move — keep only positive weights, first normalize
to sum=1.0, then truncate with `int(w * 65535)` (not rounding). Changing it to
`round()` would shift every miner's weight by 1 u16 unit, which no longer matches
the expected value the backend computes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

U16_MAX = 65535
"""Fixed-point ceiling for Bittensor weights."""


@dataclass(frozen=True)
class NormalizedWeights:
    """Normalization result.

    `uids` and `weights` correspond one-to-one, and their order is the order
    submitted on chain.
    """

    uids: list[int]
    weights: list[int]
    detail: list[str]
    """Per-entry detail, written straight into the log.

    When weights are set wrong, these lines are the only evidence left.
    """


def normalize_weights(
    weights_raw: dict[str, float], hotkeys: list[str]
) -> NormalizedWeights:
    """Convert the backend's `{hotkey: weight}` into the `(uid, u16)` the chain wants.

    Args:
        weights_raw: raw weights from the backend `/api/weights`, keyed by hotkey.
        hotkeys: the hotkey list from the metagraph; the index is the uid.
    """
    positive: dict[int, float] = {}
    detail: list[str] = []
    for uid, hotkey in enumerate(hotkeys):
        weight = weights_raw.get(hotkey, 0.0)
        if weight > 0:
            positive[uid] = weight
            detail.append(f"  uid={uid:3d} hotkey={hotkey[:12]}... raw={weight:.6f}")

    if not positive:
        return NormalizedWeights([], [], ["no positive weights"])

    total = sum(positive.values())
    normed = {uid: weight / total for uid, weight in positive.items()}

    uids = list(normed.keys())
    # Red-line expression: truncate, not round.
    weights = [int(w * U16_MAX) for w in normed.values()]

    detail.append(f"  raw total={total:.6f}, normalized to sum=1.0")
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
    """Normalize, then set the weights.

    If there are no positive weights, no transaction is sent (sending one would
    only waste the fee).
    """
    normalized = normalize_weights(weights_raw, hotkeys)
    for line in normalized.detail:
        logger.info("[set_weights]%s", line)

    if not normalized.uids:
        logger.warning("[set_weights] no positive weights, skipping this round")
        return False

    logger.info(
        "[set_weights] setting %d non-zero weights | netuid=%d",
        len(normalized.uids),
        netuid,
    )
    result = subtensor.set_weights(
        wallet=wallet,
        netuid=netuid,
        uids=normalized.uids,
        weights=normalized.weights,
    )
    return _is_success(result)


def _is_success(result: Any) -> bool:
    """Decide whether the return value of set_weights means success.

    The SDK has three return shapes (the old bool, the standard `is_success`, and
    the timelock version's `success` + `error=None`); the decision order from the
    old `validator.py` is preserved as-is.
    """
    if not result:
        logger.warning("[set_weights] set_weights returned a falsy value")
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
        logger.info("[set_weights] ✅ confirmed on chain")
        return True

    message = getattr(result, "status_message", "") or str(result)
    logger.error("[set_weights] ❌ failed on chain | msg=%s", message)
    return False
