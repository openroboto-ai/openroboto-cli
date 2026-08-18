"""LIBERO 回合数据的加载与转换。

容器读到的是**转换后**的样本（`observation/image`、`actions`、`prompt` 这套键名），
不是下载下来的原始 JSON —— 这层转换在旧 `miner/trainer_vla.py` 里，搬过来时
键名一个没动。

回合的字段校验原本依赖已删除的 `protocol/types.py::VLAEpisode`：那是一个
dataclass，缺字段时 `cls(**clean)` 直接抛 `TypeError`，而调用方没接
—— 一条坏样本能让整轮训练在加载阶段崩掉。这里改成逐条校验、跳过并计数，
坏数据只损失那一条。
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
"""一条回合必须齐的字段。与旧 `EPISODE_REQUIRED_FIELDS` 相同。"""

ALLOWED_LICENSES = frozenset(
    {"CC-BY-4.0", "CC-BY-SA-4.0", "CC0-1.0", "Apache-2.0", "MIT", "OpenRail"}
)
"""数据集允许的许可证。与旧 `ALLOWED_LICENSES` 相同。"""

DEFAULT_LICENSE = "CC-BY-4.0"


def validate_episode(episode: dict[str, Any]) -> list[str]:
    """校验一条回合，返回问题列表；空列表表示可用。"""
    problems = [
        f"缺字段: {field}" for field in REQUIRED_FIELDS if episode.get(field) is None
    ]
    license_name = episode.get("license", DEFAULT_LICENSE)
    if license_name is not None and license_name not in ALLOWED_LICENSES:
        problems.append(f"许可证不被接受: {license_name}")
    return problems


def load_episodes(json_path: str) -> list[dict[str, Any]]:
    """从 JSON 文件读回合列表，跳过校验不过的条目。

    兼容三种顶层形状：列表、`{"episodes": [...]}`、`{"data": [...]}`。
    """
    with open(json_path, encoding="utf-8") as f:
        raw = json.load(f)

    if isinstance(raw, dict):
        raw = raw.get("episodes", raw.get("data", [raw]))
    if not isinstance(raw, list):
        raise ValueError(f"{json_path} 不是回合列表，实际是 {type(raw).__name__}")

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
                "跳过回合 %s：%s", sample.get("episode_id", "?"), "; ".join(problems)
            )
            continue
        episodes.append(sample)

    logger.info(
        "从 %s 读到 %d 条可用回合（跳过 %d 条）", json_path, len(episodes), skipped
    )
    return episodes


def prepare_samples(
    episodes: list[dict[str, Any]], max_episodes: int | None = None
) -> list[dict[str, Any]]:
    """把回合转成 openpi 训练样本。键名是容器侧读的，不能改。"""
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
    logger.info("准备了 %d 条 π₀.₅ 训练样本", len(samples))
    return samples
