"""Red-line guard: u16 weight normalisation.

This is the last conversion deciding where emissions go. The three steps of the old
`validator.py` (keep positive weights only -> normalise to sum=1.0 -> truncate with
`int(w * 65535)`) are preserved word for word, and pinned down here.
"""

from __future__ import annotations

from openroboto.chain.weights import U16_MAX, normalize_weights


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
