"""The LingBot sample generator's shape invariants.

`scripts/make_lingbot_sample.py` cannot be exercised end to end here: writing
the LeRobot v3.0 layout needs `lerobot==0.4.2`, which pulls torch, and this
package's dependencies are deliberately four (AGENTS.md red line #4). What is
checked instead is everything that does not need it -- and that turns out to be
where the failures are silent.

The generator states the same facts twice on purpose: once as Python constants
the data is built from, and once as the yaml robot config text that ships next
to the data. LingBot reads only the second one. When they disagree, nothing
raises: `Normalizer` broadcasts a 6-long statistic over an 8-long vector, and
`torch.cat` is happy to concatenate slices that do not cover the tensor. The
result is a model trained on wrongly normalised inputs, which looks like a bad
hyperparameter for as long as anyone cares to look.
"""

from __future__ import annotations

import importlib.util
import itertools
import re
from pathlib import Path

import numpy as np
import pytest
import yaml

_SPEC = importlib.util.spec_from_file_location(
    "make_lingbot_sample",
    Path(__file__).resolve().parent.parent / "scripts" / "make_lingbot_sample.py",
)
assert _SPEC is not None and _SPEC.loader is not None
sample = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(sample)


def test_robot_config_slices_tile_the_declared_feature_dims() -> None:
    """Every raw dimension is mapped exactly once -- no gap, no overlap.

    A gap means the model never sees that dimension (a dropped gripper reads as
    a gripper that never moves); an overlap means it sees it twice under two
    different unified names.
    """
    widths = {"observation.state": sample.STATE_DIM, "action": sample.ACTION_DIM}
    covered: dict[str, list[tuple[int, int]]] = {key: [] for key in widths}
    for raw_key, start, end in sample.config_slices().values():
        assert raw_key in widths, f"robot config maps unknown raw key {raw_key}"
        covered[raw_key].append((start, end))

    for raw_key, width in widths.items():
        spans = sorted(covered[raw_key])
        assert spans, f"{raw_key} is not mapped by the robot config at all"
        assert spans[0][0] == 0
        assert spans[-1][1] == width, f"{raw_key} slices stop at {spans[-1][1]}"
        for (_, end), (next_start, _) in itertools.pairwise(spans):
            assert end == next_start, f"{raw_key} has a gap or overlap at {end}"


def test_robot_config_maps_the_camera_keys_the_dataset_declares() -> None:
    """The raw camera names in the yaml are the ones written into `info.json`.

    These are also the names real LIBERO uses, which is the whole point of the
    sample: renaming one side leaves a config that loads the sample and fails on
    the real dataset.
    """
    images = yaml.safe_load(sample.ROBOT_CONFIG)["images"]
    raw_keys = [next(iter(entry.values()))["origin_keys"] for entry in images]
    assert raw_keys == list(sample.CAMERA_KEYS)


def test_norm_stats_are_one_vector_per_mapped_dimension() -> None:
    """Statistic length == slice width, for every statistic the vendor reads.

    `Normalizer` picks its pair by `norm_type`, so a wrong length in any one of
    the six only shows up under the configuration that happens to read it.
    """
    values = np.arange(60, dtype=np.float32).reshape(10, 6)
    stats = sample._norm_stats(values[:, 1:4])
    assert set(stats) == {"mean", "std", "q01", "q99", "q02", "q98"}
    for name, vector in stats.items():
        assert len(vector) == 3, f"{name} has {len(vector)} entries, expected 3"


def test_trajectory_stays_inside_the_measured_libero_range() -> None:
    """Values outside the range make the shipped norm stats a lie.

    The action rescaling is the fragile part: it divides by a per-dimension
    standard deviation, and getting the scale wrong pins the data to the clip
    bounds, where `bounds_99_woclip` normalisation degenerates.
    """
    state, action = sample._trajectory(np.random.default_rng(0), 128)
    assert state.shape == (128, sample.STATE_DIM)
    assert action.shape == (128, sample.ACTION_DIM)
    assert state.dtype == np.float32 and action.dtype == np.float32
    assert np.all(state >= sample.STATE_MIN - 1e-6)
    assert np.all(state <= sample.STATE_MAX + 1e-6)
    assert np.all(action >= sample.ACTION_MIN - 1e-6)
    assert np.all(action <= sample.ACTION_MAX + 1e-6)

    # Non-degenerate normalisation: q99 must actually sit above q01 per dim.
    low, high = np.quantile(action, [0.01, 0.99], axis=0)
    assert np.all(high - low > 1e-3)


def test_episode_plan_is_deterministic_and_straddles_the_chunk_size() -> None:
    """Same seed, same plan -- the hosted artifact has to be reproducible.

    The length spread is load-bearing too: LingBot's default `chunk_size` is 50,
    and a sample entirely below it would only ever exercise the `action_is_pad`
    branch.
    """
    plan = sample._episode_plan()
    assert plan == sample._episode_plan()
    assert len(plan) == sample.NUM_EPISODES

    counts: dict[str, int] = {}
    for suite, _instruction, _length in plan:
        counts[suite] = counts.get(suite, 0) + 1
    assert counts == dict(sample.SUITES)

    lengths = [length for _suite, _instruction, length in plan]
    assert min(lengths) < 50 < max(lengths)


def test_build_says_what_to_install_when_lerobot_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The failure a first-time runner hits must name the pinned version.

    `lerobot` is not a dependency of this package and never will be; the message
    is the only thing standing between "ImportError" and installing whatever
    version pip resolves today -- which for anything past v0.4.2 may write a
    layout LingBot's pin cannot read.
    """
    monkeypatch.setitem(__import__("sys").modules, "lerobot.datasets", None)
    monkeypatch.setattr(
        __import__("builtins"),
        "__import__",
        _raise_on_lerobot(__import__("builtins").__import__),
    )
    with pytest.raises(SystemExit, match=re.escape("lerobot==0.4.2")):
        sample.build(tmp_path / "out")


def _raise_on_lerobot(real_import):  # type: ignore[no-untyped-def]
    def guarded(name, *args, **kwargs):  # type: ignore[no-untyped-def]
        if name.startswith("lerobot"):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    return guarded
