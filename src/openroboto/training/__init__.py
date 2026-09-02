"""Training: container invocation, data preparation, single-run orchestration."""

from __future__ import annotations

from openroboto.training.container import (
    TrainingError,
    build_docker_command,
    run_training,
    runner_image,
)
from openroboto.training.dataset import load_episodes, prepare_samples
from openroboto.training.run import (
    TrainOutcome,
    TrainParams,
    download_dataset,
    train_once,
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
    "train_once",
]
