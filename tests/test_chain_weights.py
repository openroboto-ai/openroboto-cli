"""Red-line guard: u16 weight normalisation.

This is the last conversion deciding where emissions go. The three steps (keep
positive weights only -> normalise to sum=1.0 -> truncate with `int(w * 65535)`)
came from the old `validator.py` and are preserved exactly; the implementation
now lives in `openroboto.chain.weights`, and this file pins the arithmetic.
"""

from __future__ import annotations

from types import SimpleNamespace

from openroboto.chain.weights import (
    REFUSE_ABOVE_DROPPED_SHARE,
    U16_MAX,
    normalize_weights,
    set_weights_on_chain,
)


def test_only_positive_weights_are_kept() -> None:
    result = normalize_weights(
        {"a": 1.0, "b": 0.0, "c": -3.0}, ["a", "b", "c", "unknown"]
    )
    assert result.uids == [0]
    assert result.weights == [U16_MAX]


def test_weights_are_normalised_then_truncated_not_rounded() -> None:
    """1/3 * 65535 = 21845.0; 2/3 * 65535 = 43690.0 -- truncation and rounding happen to
    agree here, so a diverging pair is used instead: 0.7/0.3."""
    result = normalize_weights({"a": 0.7, "b": 0.3}, ["a", "b"])
    # 0.7 * 65535 = 45874.5 -- truncation gives 45874, rounding would give 45875.
    assert result.weights == [45874, 19660]
    # truncation necessarily loses a few units; this is the existing behaviour
    assert sum(result.weights) < U16_MAX


def test_uid_is_the_metagraph_index_not_the_dict_order() -> None:
    result = normalize_weights({"c": 1.0, "a": 1.0}, ["a", "b", "c"])
    assert result.uids == [0, 2]
    assert result.weights == [32767, 32767]


def test_no_positive_weights_returns_empty_with_reason() -> None:
    result = normalize_weights({"a": 0.0}, ["a"])
    assert result.uids == []
    assert result.weights == []
    assert result.detail and "positive" in result.detail[0].lower()


def test_detail_lines_cover_every_weight() -> None:
    """The detail lines are the only evidence left when weights are set wrong, so every
    participating uid must appear."""
    result = normalize_weights({"a": 1.0, "b": 3.0}, ["a", "b"])
    joined = "\n".join(result.detail)
    assert "uid=  0" in joined
    assert "uid=  1" in joined
    assert "sum=1.0" in joined


# ─── the refusal that protects the burn address ──────────────


class _RecordingSubtensor:
    """Records whether `set_weights` was reached at all."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def set_weights(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        return SimpleNamespace(is_success=True)


def test_weights_are_refused_when_most_of_the_share_would_be_dropped() -> None:
    """🔴 The whole point of `REFUSE_ABOVE_DROPPED_SHARE`.

    A normal snapshot puts ~0.9 on the burn address. If that hotkey is missing
    from the metagraph, normalisation hands its share to the miners that *are*
    there -- the emission that should have been destroyed is paid out instead,
    ten times over, by an extrinsic that looks perfectly ordinary. Nothing
    downstream can tell that apart from a legitimate result, so the refusal has
    to happen here.

    Refusing costs one cycle: the chain keeps the previous weights, which were
    correct.
    """
    subtensor = _RecordingSubtensor()
    sent = set_weights_on_chain(
        subtensor,
        wallet=object(),
        netuid=80,
        # 0.9 belongs to a hotkey this subnet does not have.
        weights_raw={"burn": 0.9, "miner": 0.1},
        hotkeys=["miner"],
    )

    assert sent is False
    assert subtensor.calls == [], "an extrinsic was sent that redistributed 90%"


def test_a_single_deregistered_miner_does_not_stop_the_cycle() -> None:
    """The other side of the same threshold: refusing everything would be its
    own outage. One miner dropping off is a normal event and must go through."""
    subtensor = _RecordingSubtensor()
    sent = set_weights_on_chain(
        subtensor,
        wallet=object(),
        netuid=80,
        weights_raw={"burn": 0.9, "gone": 0.07, "here": 0.03},
        hotkeys=["burn", "here"],
    )

    assert sent is True
    assert len(subtensor.calls) == 1


def test_the_threshold_sits_between_those_two_cases() -> None:
    """Pinned because both neighbours above are asserted against it: a value
    below 0.07 would break normal cycles, one above 0.9 would let the burn
    address vanish silently."""
    assert 0.07 < REFUSE_ABOVE_DROPPED_SHARE < 0.9
