"""Loading and conversion of LIBERO episode data.

What the container reads is the **converted** samples (the `observation/image`,
`actions`, `prompt` set of key names), not the raw JSON that was downloaded — that
conversion layer lived in the old `miner/trainer_vla.py`, and not one key name was
touched when it was moved here.

Field validation for episodes used to depend on the now-deleted
`protocol/types.py::VLAEpisode`: that was a dataclass, so a missing field made
`cls(**clean)` raise `TypeError` outright, and the caller did not catch it — one
bad sample could crash a whole training run at the loading stage. This was
changed to validate entry by entry, skipping and counting, so bad data only costs
you that one entry.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

REQUIRED_FIELDS = (
    "episode_id",
    "timestamp",
    "observation",
    "action",
    "language_instruction",
    "license",
)
"""Fields an episode must have. Same as the old `EPISODE_REQUIRED_FIELDS`."""

ALLOWED_LICENSES = frozenset(
    {"CC-BY-4.0", "CC-BY-SA-4.0", "CC0-1.0", "Apache-2.0", "MIT", "OpenRail"}
)
"""Licenses accepted for datasets. Same as the old `ALLOWED_LICENSES`."""

DEFAULT_LICENSE = "CC-BY-4.0"


def validate_episode(episode: dict[str, Any]) -> list[str]:
    """Validate one episode and return the list of problems; empty means usable."""
    problems = [
        f"missing field: {field}"
        for field in REQUIRED_FIELDS
        if episode.get(field) is None
    ]
    license_name = episode.get("license", DEFAULT_LICENSE)
    if license_name is not None and license_name not in ALLOWED_LICENSES:
        problems.append(f"license not accepted: {license_name}")
    return problems


def load_episodes(json_path: str) -> list[dict[str, Any]]:
    """Read the episode list from a JSON file, skipping entries that fail validation.

    Three top-level shapes are accepted: a list, `{"episodes": [...]}`, and
    `{"data": [...]}`.
    """
    with open(json_path, encoding="utf-8") as f:
        raw = json.load(f)

    if isinstance(raw, dict):
        raw = raw.get("episodes", raw.get("data", [raw]))
    if not isinstance(raw, list):
        raise ValueError(
            f"{json_path} is not a list of episodes, got {type(raw).__name__}"
        )

    episodes: list[dict[str, Any]] = []
    skipped = 0
    for sample in raw:
        if not isinstance(sample, dict):
            skipped += 1
            continue
        problems = validate_episode(sample)
        if problems:
            skipped += 1
            logger.warning(
                "Skipping episode %s: %s",
                sample.get("episode_id", "?"),
                "; ".join(problems),
            )
            continue
        episodes.append(sample)

    logger.info(
        "Read %s: %d usable episodes (%d skipped)", json_path, len(episodes), skipped
    )
    return episodes


def prepare_samples(
    episodes: list[dict[str, Any]], max_episodes: int | None = None
) -> list[dict[str, Any]]:
    """Convert episodes into openpi training samples.

    The key names are what the container side reads and must not be changed.
    """
    if max_episodes:
        episodes = episodes[:max_episodes]

    samples: list[dict[str, Any]] = []
    for episode in episodes:
        observation = episode.get("observation", {}) or {}
        samples.append(
            {
                "observation/image": observation.get("image", []),
                "observation/wrist_image": observation.get("wrist_image", []),
                "observation/state": observation.get("state", []),
                "actions": episode.get("action", []),
                "prompt": episode.get("language_instruction", ""),
            }
        )
    logger.info("Prepared %d π₀.₅ training samples", len(samples))
    return samples
