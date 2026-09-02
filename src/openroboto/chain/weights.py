"""Sending weights on chain, for external validators.

The normalisation itself is **not here** -- it lives in
`openroboto_protocol.weights`, shared with the backend's chain-writer. That is
the whole point of the protocol package: both sides can be shown to install the
same code, rather than being observed to agree.

⚠️ Do not reintroduce a local `normalize_weights`. Two copies of an expression
whose floating-point shape *is* the behaviour is how "a cleanup in one of them,
months apart, silently changes who gets paid" happens. The red-line reasoning
(strict `> 0`, share-then-scale, `int()` truncation) is documented there.

What stays here is what genuinely differs between the two callers: the
`set_weights` call and how this SDK's return shapes are read.
"""

from __future__ import annotations

import logging
from typing import Any

from openroboto_protocol.weights import U16_MAX, NormalizedWeights, normalize_weights

__all__ = ["U16_MAX", "NormalizedWeights", "normalize_weights", "set_weights_on_chain"]

#: Refuse to send when this fraction of the incoming share belonged to hotkeys
#: absent from the metagraph.
#:
#: A normal snapshot puts 0.9 on the burn address and splits 0.1 across the top
#: three miners. So a deregistered miner costs at most 0.07, while losing the
#: burn address costs 0.9 -- and losing it means the emission that should have
#: been destroyed is paid out to miners instead, ten times over, with a
#: perfectly ordinary-looking extrinsic. 0.5 sits between the two with room on
#: both sides.
#:
#: Refusing costs one cycle: the chain keeps the previous weights, which were
#: correct. Sending costs that cycle's entire emission, and is only visible
#: afterwards.
REFUSE_ABOVE_DROPPED_SHARE = 0.5

logger = logging.getLogger(__name__)


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

    Nothing is sent either when most of the incoming share belonged to hotkeys
    that are not on this subnet -- see `REFUSE_ABOVE_DROPPED_SHARE`.
    """
    normalized = normalize_weights(weights_raw, hotkeys)
    for line in normalized.detail:
        logger.info("[set_weights]%s", line)

    if not normalized.uids:
        logger.warning("[set_weights] no positive weights, skipping this cycle")
        return False

    if normalized.dropped_share >= REFUSE_ABOVE_DROPPED_SHARE:
        logger.error(
            "[set_weights] %.0f%% of the incoming weight belongs to hotkeys that are "
            "not on netuid %d, and would be redistributed to the ones that are. "
            "Refusing to send. The chain keeps the previous weights.",
            normalized.dropped_share * 100,
            netuid,
        )
        logger.error(
            "[set_weights] This usually means the backend is publishing weights for "
            "a different subnet than the one this validator is pointed at. Check "
            "`netuid` and `network` in the config against the backend it is reading."
        )
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
