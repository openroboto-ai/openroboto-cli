"""红线守卫：u16 权重归一化。

这是排放去向的最后一道换算。旧 `validator.py` 的三步（只留正权重 → 归一到
sum=1.0 → `int(w * 65535)` 截断）逐字保留，这里把它钉死。
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
    """1/3 * 65535 = 21845.0；2/3 * 65535 = 43690.0 —— 截断与四舍五入在这里
    恰好同值，所以再用一组会分叉的：0.7/0.3。"""
    result = normalize_weights({"a": 0.7, "b": 0.3}, ["a", "b"])
    # 0.7 * 65535 = 45874.5 —— 截断给 45874，四舍五入会给 45875。
    assert result.weights == [45874, 19660]
    assert sum(result.weights) < U16_MAX  # 截断必然少几个单位，这是既有行为


def test_uid_is_the_metagraph_index_not_the_dict_order() -> None:
    result = normalize_weights({"c": 1.0, "a": 1.0}, ["a", "b", "c"])
    assert result.uids == [0, 2]
    assert result.weights == [32767, 32767]


def test_no_positive_weights_returns_empty_with_reason() -> None:
    result = normalize_weights({"a": 0.0}, ["a"])
    assert result.uids == []
    assert result.weights == []
    assert result.detail == ["没有正权重"]


def test_detail_lines_cover_every_weight() -> None:
    """明细是权重设错时唯一的现场，每个参与的 uid 都要出现。"""
    result = normalize_weights({"a": 1.0, "b": 3.0}, ["a", "b"])
    joined = "\n".join(result.detail)
    assert "uid=  0" in joined
    assert "uid=  1" in joined
    assert "sum=1.0" in joined
