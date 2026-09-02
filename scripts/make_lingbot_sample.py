#!/usr/bin/env python3
"""Build the LingBot sample dataset: a 50-episode LeRobot **v3.0** directory.

`https://api.openroboto.ai/train.json` is the π0.5 sample and stays exactly as
it is -- it is served to miners on the live simulation competition today, and
`training/dataset.py` reads its key names. This script produces the *second*
sample, the one the LingBot competitions need, and it produces a different kind
of object: not a JSON file, a **directory**.

Why a directory
---------------
LingBot-VLA 2.0 has exactly one dataset entry point. `lingbotvla/data/dataset.py
::build_vla_dataset` builds either `VLADataset` or `MultiVLADataset`, and both
end at `lerobot`'s `LeRobotDataset` (`lingbotvla/data/vla_data/base_dataset.py`
:200-213). `data.train_path` is either a local LeRobot directory or a Hugging
Face repo id -- `_resolve_lerobot_location` decides by asking whether the path
exists on disk. There is no JSON reader anywhere under `lingbotvla/data/`, and
no hook to add one.

Why v3.0 and not v2.1
---------------------
LingBot's own `lingbotvla/data/vla_data/README.md:10` says "Both LeRobot v2.1
and LeRobot v3.0 layouts are supported directly". **That sentence is wrong for
the version their installer pins.** `tools/create_train_env.sh:132-133` installs
`lerobot @ v0.4.2`, whose `CODEBASE_VERSION` is `"v3.0"`
(`src/lerobot/datasets/lerobot_dataset.py:80`), and `load_metadata` runs
`check_version_compatibility` on every load. A v2.1 dataset raises
`BackwardCompatibilityError`; a v2.0 one -- which is what the widely used
`physical-intelligence/libero` conversion is -- does not even get a message,
it raises `NotImplementedError("Contact the maintainer on Discord")` out of
`backward_compatibility.py:47`.

So v3.0 is not a preference here, it is the only layout that loads. Emitting
v2.1 would produce a sample that fails on the miner's first command with an
error naming a Discord server.

What "isomorphic" means here, and why this generates rather than converts
------------------------------------------------------------------------
The sample's whole job is to let a miner rehearse the run they will do for
real. That only proves anything if the rehearsal and the real thing have the
same shape -- same layout version, same feature keys, same dtypes -- so that
the robot config written against the sample keeps working when `train_path` is
repointed at the real dataset.

The real dataset for these competitions is LIBERO, which miners fetch
themselves (`README.md:6`), and the LeRobot v3.0 conversion of it is
`HuggingFaceVLA/libero`: 1693 episodes, 273465 frames, `robot_type: panda`,
fps 10. Every feature name, dtype and shape below is copied from that
dataset's `meta/info.json`. One `libero.yaml` robot config therefore covers
both this sample and the real thing.

Converting the existing `train.json` instead was the other option, and it does
not survive contact with its own input. That file carries **one** frame per
episode against 35-70 actions, so a converter would have to invent 97% of every
episode -- that is generation wearing a converter's coat. Worse, its
`observation.state` is 8, 10, 12 or 14 dims depending on the episode, and a
LeRobot `info.json` declares one fixed shape per feature for the whole dataset,
so there is no honest number to write down. And its images are not images: the
entries say `"encoding": "jpeg_base64"` but the bytes decode to 0,1,2,...,255
repeating, not to a JPEG. There is no real content in there to preserve.

Who eats this, and who does not
-------------------------------
🔴 **Not `openroboto train`.** `runner/lingbot/train_runner.py` deliberately
does not use the vendor's data pipeline (its decision 4): it keeps `episodes` as
the decoded JSON list red line #2 has always passed, and leaves batching and
normalisation to the strategy script. Under that runner the π0.5 `train.json` is
still the input, and this directory is unused.

This sample is for the **other** route, which is the only one that works today:
`sim_lingbot` is `UNAVAILABLE`, so `openroboto train` refuses, and the advice it
prints is "train it however you like, then `openroboto check` and `openroboto
submit`". Training it however you like means the vendor's
`tasks/vla/train_lingbotvla.py`, and that reads a LeRobot directory and nothing
else. It stays the route for a full-parameter fine-tune -- the vendor's own
recipe -- even if the LoRA runner is verified and switched on.

So the two samples are not competing versions of one thing; they feed two
different trainers, and both need to exist as long as both routes do. **Which
one the subnet hosts and advertises is a product decision, not this script's**
-- see the hosting section of `docs/lingbot-sample-dataset.md`.

⚠️ Two defects in the π0.5 `train.json` matter for the runner route and are out
of scope here, because that file is served to live miners and is not ours to
change: its images claim `"encoding": "jpeg_base64"` but decode to raw bytes
that are not a JPEG, and its `observation.state` is 8, 10, 12 or 14 dims
depending on the episode. A strategy script that believes either label breaks.

Usage
-----
    pip install "lerobot==0.4.2"      # the version LingBot pins; nothing else
    python scripts/make_lingbot_sample.py --out dist/lingbot-sample

`lerobot` is used as the **writer**, not just the reader, on purpose. The v3.0
layout is parquet plus chunk/file bookkeeping (`meta/episodes/`,
`dataset_from_index`, `data/chunk_index`, ...) whose only specification is that
library's source. Hand-rolling a serializer would mean tracking it forever and
finding out about drift from a miner. This script is not part of the wheel and
not run by miners; it is run once to produce an artifact that gets hosted.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import yaml

REPO_ID = "openroboto/lingbot-sample"
"""Only a name. The dataset is loaded from a local path or re-uploaded under
whatever id it ends up hosted at; nothing resolves this string."""

FPS = 10
ROBOT_TYPE = "panda"
IMAGE_SHAPE = (256, 256, 3)
STATE_DIM = 8
ACTION_DIM = 7
NUM_EPISODES = 50
SEED = 20260826

CAMERA_KEYS = ("observation.images.image", "observation.images.image2")
"""Verbatim from `HuggingFaceVLA/libero`'s `meta/info.json`.

`image2` is the wrist camera and the name says nothing about that, which is
tempting to improve. Do not: a robot config maps *raw* keys, so renaming it
here would make the config that works on this sample fail on the real dataset
-- the exact class of surprise this sample exists to prevent.
"""

# Per-dimension bounds measured from `HuggingFaceVLA/libero`'s `meta/stats.json`
# (fetched 2026-08-26). The trajectories below are synthetic, but they are
# synthetic *inside the real range*, so the norm stats this script writes are in
# the same ballpark as the ones a miner computes on real LIBERO -- a sample
# whose values are all ~1e3 would train to a different normalization and hide
# scale bugs until the real run.
#
# state: end-effector xyz (3) + axis-angle (3) + gripper qpos (2)
# action: delta end-effector pose (6) + gripper open/close (1)
STATE_MIN = np.array([-0.483, -0.326, 0.008, 0.353, -3.641, -1.843, -0.001, -0.042])
STATE_MAX = np.array([0.210, 0.391, 1.366, 3.671, 3.561, 1.386, 0.042, 0.001])
ACTION_MIN = np.array([-0.938, -0.938, -0.938, -0.258, -0.375, -0.368, -1.0])
ACTION_MAX = np.array([0.938, 0.938, 0.938, 0.356, 0.375, 0.375, 1.0])
ACTION_Q01 = np.array([-0.535, -0.604, -0.778, -0.074, -0.116, -0.126, -1.0])
ACTION_Q99 = np.array([0.709, 0.732, 0.801, 0.084, 0.139, 0.109, 0.919])

# The same four suites and twenty instructions the π0.5 `train.json` carries, in
# the same 13/13/12/12 split. Keeping them identical is what lets a miner hold
# the two samples side by side and see that only the container changed.
SUITES = (
    ("libero_spatial", 13),
    ("libero_object", 13),
    ("libero_goal", 12),
    ("libero_10", 12),
)
INSTRUCTIONS = (
    "pick up the red block and place it on the blue plate",
    "open the top drawer of the cabinet",
    "put the bowl on the stove",
    "close the lid of the container",
    "move the cup from the right side to the left side of the table",
    "stack the small box on top of the large box",
    "pick up the banana and put it in the basket",
    "turn on the light switch",
    "align the two blocks side by side",
    "place the mug under the coffee machine",
    "grasp the hammer and tap the nail twice",
    "move the plate to the center of the table",
    "put the milk carton into the fridge",
    "pick up the book and place it on the shelf",
    "slide the tray towards the front edge of the table",
    "grasp the spoon and stir the bowl counter-clockwise",
    "pick up the bottle and place it next to the glass",
    "push the chair under the table",
    "open the microwave door",
    "place the towel over the rail",
)

MIN_EPISODE_LEN = 40
MAX_EPISODE_LEN = 200
"""Real LIBERO averages ~161 frames per episode, and LingBot's default
`chunk_size` is 50. A range that straddles 50 is deliberate: episodes above it
exercise the ordinary path, and the short ones exercise `action_is_pad`, which
is the branch that silently trains on padding if it is wrong. The π0.5 sample's
own 35-70 range would have put *every* episode on the padded side."""


def _episode_plan() -> list[tuple[str, str, int]]:
    """(suite, instruction, length) for each episode. Deterministic."""
    rng = np.random.default_rng(SEED)
    suites = [name for name, count in SUITES for _ in range(count)]
    if len(suites) != NUM_EPISODES:
        raise ValueError(f"suite counts sum to {len(suites)}, expected {NUM_EPISODES}")
    lengths = rng.integers(MIN_EPISODE_LEN, MAX_EPISODE_LEN + 1, size=NUM_EPISODES)
    return [
        (suite, INSTRUCTIONS[index % len(INSTRUCTIONS)], int(length))
        for index, (suite, length) in enumerate(zip(suites, lengths, strict=True))
    ]


def _trajectory(rng: np.random.Generator, length: int) -> tuple[np.ndarray, np.ndarray]:
    """A smooth state trajectory and the action stream, both inside LIBERO's range.

    Smooth, not white noise: `q01`/`q99` over independent uniform samples land on
    the bounds themselves, which makes `bounds_99_woclip` normalization a no-op
    and hides any mistake in the norm stats path.
    """
    phase = rng.uniform(0.0, 2 * np.pi, size=STATE_DIM)
    speed = rng.uniform(0.4, 1.6, size=STATE_DIM)
    t = np.linspace(0.0, 2 * np.pi, length, dtype=np.float64)[:, None]
    wave = np.sin(t * speed + phase)  # (length, STATE_DIM) in [-1, 1]
    state = STATE_MIN + (wave + 1.0) / 2.0 * (STATE_MAX - STATE_MIN)

    # Actions are deltas of the end-effector pose, so derive them from the state
    # rather than drawing them independently: an action stream uncorrelated with
    # the state it follows is the one thing a VLA sample must not teach.
    #
    # The delta is then mapped onto LIBERO's measured q01..q99 span. A raw
    # per-frame delta is nowhere near the command scale -- the rotation dims came
    # out an order of magnitude past `ACTION_MAX`, and the clip turned them into a
    # square wave that leaves `q01`/`q99` sitting on the clip bounds with the
    # action carrying nothing but a sign. Matching the real quantiles rather than
    # the real std is deliberate: the quantiles are what `bounds_99_woclip`
    # actually reads, so the sample's normalisation lands where the real one
    # does, and the clip below stays a guard instead of shaping the data.
    delta = np.diff(state[:, :6], axis=0, prepend=state[:1, :6])
    low, high = np.quantile(delta, [0.01, 0.99], axis=0)
    span = ACTION_Q99[:6] - ACTION_Q01[:6]
    delta = (delta - low) / (high - low + 1e-12) * span + ACTION_Q01[:6]
    gripper = np.where(np.sin(t[:, 0] * 2.0 + phase[0]) > 0, 1.0, -1.0)
    action = np.concatenate([delta, gripper[:, None]], axis=1)
    action = np.clip(action, ACTION_MIN, ACTION_MAX)
    return state.astype(np.float32), action.astype(np.float32)


def _frame_images(rng: np.random.Generator, length: int) -> np.ndarray:
    """(length, 2, H, W, 3) uint8 -- a gradient background with a marker that moves.

    Obviously synthetic on sight, which is the point: nobody should be able to
    mistake this for LIBERO and report a score from it. Flat gradients also make
    each PNG a couple of kB, which is what keeps the artifact near the 20 MB the
    π0.5 sample already costs.
    """
    height, width, _ = IMAGE_SHAPE
    ramp_y = np.linspace(0, 255, height, dtype=np.uint8)[:, None]
    ramp_x = np.linspace(0, 255, width, dtype=np.uint8)[None, :]
    base = np.zeros((height, width, 3), dtype=np.uint8)
    base[..., 0] = ramp_y
    base[..., 1] = ramp_x
    base[..., 2] = 64

    tint = rng.integers(0, 96, size=2)
    frames = np.empty((length, 2, height, width, 3), dtype=np.uint8)
    box = 24
    for step in range(length):
        travel = step / max(1, length - 1)
        for cam in range(2):
            image = base.copy()
            image[..., 2] = np.uint8(64 + tint[cam])
            top = int(travel * (height - box))
            left = int((1.0 - travel) * (width - box)) if cam else top
            image[top : top + box, left : left + box] = 255
            frames[step, cam] = image
    return frames


def _norm_stats(values: np.ndarray) -> dict[str, list[float]]:
    """The six statistics `lingbotvla.utils.normalize.NormStats` serializes.

    ponytail: exact quantiles via `np.quantile` instead of reimplementing
    `RunningStats`' streaming histogram. The consumer
    (`vla_data/transform.py::Normalizer`) reads whichever pair its `norm_type`
    names and does arithmetic with it; it has no opinion on how they were
    estimated, and on 50 episodes the whole array fits in memory anyway. If a
    future sample stops fitting, run LingBot's own `scripts/compute_norm_stats.py`
    instead of growing this function.
    """
    quantiles = np.quantile(values, [0.01, 0.99, 0.02, 0.98], axis=0)
    return {
        "mean": values.mean(axis=0).tolist(),
        "std": values.std(axis=0).tolist(),
        "q01": quantiles[0].tolist(),
        "q99": quantiles[1].tolist(),
        "q02": quantiles[2].tolist(),
        "q98": quantiles[3].tolist(),
    }


ROBOT_CONFIG = """\
# Robot config for the LIBERO feature layout, as LingBot-VLA 2.0 expects it.
#
# The file name without `.yaml` is what `data.data_name` must be set to, and
# `data.robot_config_root` must be the directory holding it
# (`lingbotvla/data/vla_data/base_dataset.py:187`).
#
# Every raw key below is verbatim from `HuggingFaceVLA/libero`'s meta/info.json,
# which is the LeRobot v3.0 conversion of the real benchmark. That is what makes
# this file work unchanged on both the sample and the real dataset -- repoint
# `data.train_path` and nothing else moves.
#
# 🔴 The slice boundaries encode LIBERO's state layout: end-effector xyz [0:3)
# plus axis-angle [3:6), then the two gripper finger positions [6:8). The
# action is a delta end-effector pose [0:6) plus one gripper command [6:7).
#
# `subtract_state: False` on both, i.e. absolute targets. For `end.position`
# that is what LingBot's own README recommends ("we recommend learning absolute
# action, as relative rotation computation is not currently supported"), and for
# `effector.position` it is not a choice at all -- `vla_data/utils.py:236`
# asserts it.
states:
  - observation.state.end.position:
      origin_keys:
        - observation.state:
            start: 0
            end: 6
  - observation.state.effector.position:
      origin_keys:
        - observation.state:
            start: 6
            end: 8

actions:
  - action.end.position:
      origin_keys:
        - action:
            start: 0
            end: 6
      subtract_state: False
  - action.effector.position:
      origin_keys:
        - action:
            start: 6
            end: 7
      subtract_state: False

# LIBERO is a single-arm setup with two cameras: the scene view and the wrist.
# ⚠️ Which unified camera slot the wrist maps to (`camera_wrist_right` here) is
# a modelling choice, not something the data decides -- the pretrained model was
# trained with top / wrist_left / wrist_right. Whatever is chosen must also
# appear in `data.cameras` of the training config or `FeatureTransform` raises.
images:
  - observation.images.camera_top:
      origin_keys: observation.images.image
  - observation.images.camera_wrist_right:
      origin_keys: observation.images.image2

norm_stats: norm_stats/libero.json
"""


def config_slices() -> dict[str, tuple[str, int, int]]:
    """`unified feature -> (raw key, start, end)`, read out of `ROBOT_CONFIG` itself.

    The norm stats have to be computed over the *converted* features -- the ones
    the robot config produces, not the raw `observation.state` -- so the same
    slice boundaries are needed twice. Parsing them back out of the yaml instead
    of writing `state[:, :6]` a second time is what keeps the two from drifting:
    a mismatch there is silent, because `Normalizer` broadcasts a 6-long stat
    over an 8-long vector without complaining.
    """
    config = yaml.safe_load(ROBOT_CONFIG)
    slices: dict[str, tuple[str, int, int]] = {}
    for category in ("states", "actions"):
        for entry in config[category]:
            ((feature, info),) = entry.items()
            ((origin,),) = (item.items() for item in info["origin_keys"])
            raw_key, bounds = origin
            slices[feature] = (raw_key, int(bounds["start"]), int(bounds["end"]))
    return slices


def build(out: Path) -> Path:
    """Write the dataset, the robot config and the norm stats under `out`."""
    try:
        from lerobot.datasets.lerobot_dataset import LeRobotDataset
    except ImportError:
        raise SystemExit(
            "lerobot is not installed. This script writes the LeRobot v3.0 layout\n"
            "with the same library that reads it, so that the two cannot drift:\n"
            '    pip install "lerobot==0.4.2"   # the version LingBot pins'
        ) from None

    features = {
        key: {
            "dtype": "image",
            "shape": IMAGE_SHAPE,
            "names": ["height", "width", "channel"],
        }
        for key in CAMERA_KEYS
    }
    features["observation.state"] = {
        "dtype": "float32",
        "shape": (STATE_DIM,),
        "names": ["state"],
    }
    features["action"] = {
        "dtype": "float32",
        "shape": (ACTION_DIM,),
        "names": ["actions"],
    }

    dataset_root = out / "dataset"
    if dataset_root.exists():
        raise SystemExit(
            f"{dataset_root} already exists -- remove it or pick another --out"
        )

    dataset = LeRobotDataset.create(
        repo_id=REPO_ID,
        fps=FPS,
        features=features,
        root=dataset_root,
        robot_type=ROBOT_TYPE,
        # 🔴 Not videos. Real LIBERO in LeRobot stores its cameras as `dtype:
        # image` (PNG embedded in the parquet, `total_videos: 0`), so mp4 keys
        # here would send the sample down `_query_videos` and torchcodec while
        # the real dataset never goes near them.
        use_videos=False,
    )

    rng = np.random.default_rng(SEED)
    all_state: list[np.ndarray] = []
    all_action: list[np.ndarray] = []
    for suite, instruction, length in _episode_plan():
        state, action = _trajectory(rng, length)
        images = _frame_images(rng, length)
        for step in range(length):
            dataset.add_frame(
                {
                    CAMERA_KEYS[0]: images[step, 0],
                    CAMERA_KEYS[1]: images[step, 1],
                    "observation.state": state[step],
                    "action": action[step],
                    # LeRobot has one language field, so the suite goes in the
                    # instruction the way LIBERO's own conversions do it.
                    "task": f"{instruction} ({suite})",
                }
            )
        dataset.save_episode()
        all_state.append(state)
        all_action.append(action)
    dataset.finalize()

    raw = {
        "observation.state": np.concatenate(all_state),
        "action": np.concatenate(all_action),
    }
    stats = {
        feature: _norm_stats(raw[raw_key][:, start:end])
        for feature, (raw_key, start, end) in config_slices().items()
    }
    norm_path = out / "norm_stats" / "libero.json"
    norm_path.parent.mkdir(parents=True, exist_ok=True)
    norm_path.write_text(
        json.dumps(
            {"norm_stats": stats, "count": len(raw["observation.state"])}, indent=2
        ),
        encoding="utf-8",
    )

    config_path = out / "robot_configs" / "libero.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(ROBOT_CONFIG, encoding="utf-8")
    return dataset_root


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--out",
        default="dist/lingbot-sample",
        help="directory to write dataset/, robot_configs/ and norm_stats/ into",
    )
    args = parser.parse_args(argv)

    out = Path(args.out)
    root = build(out)
    total = sum(f.stat().st_size for f in out.rglob("*") if f.is_file())
    print(f"wrote {out} ({total / 1024 / 1024:.1f} MB)")
    print(f"  dataset      {root}")
    print(f"  robot config {out / 'robot_configs' / 'libero.yaml'}")
    print(f"  norm stats   {out / 'norm_stats' / 'libero.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
