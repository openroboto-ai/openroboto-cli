"""训练：容器调用、数据准备、单轮编排。"""

from __future__ import annotations

from openroboto.training.container import (
    TrainingError,
    build_docker_command,
    run_training,
    runner_image,
)
from openroboto.training.dataset import load_episodes, prepare_samples
from openroboto.training.round import (
    TrainOutcome,
    TrainParams,
    download_dataset,
    train_round,
)

__all__ = [
    "TrainOutcome",
    "TrainParams",
    "TrainingError",
    "build_docker_command",
    "download_dataset",
    "load_episodes",
    "prepare_samples",
    "run_training",
    "runner_image",
    "train_round",
]
