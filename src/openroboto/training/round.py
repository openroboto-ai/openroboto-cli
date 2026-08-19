"""Run one training round: download data, run the container, collect metrics and
the training proof.

Corresponds to the old `miner/trainer_vla.py::train_vla`. Two artifact files, both
uploaded along with the model: `metrics.json` (training metrics) and
`training_proof.json` (the training proof).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import urllib.error
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openroboto import __version__
from openroboto.http_client import build_request, urlopen
from openroboto.training.container import run_training
from openroboto.training.dataset import load_episodes, prepare_samples

logger = logging.getLogger(__name__)

HASH_PREFIX_LEN = 16
WEIGHT_SUFFIXES = (".safetensors", ".bin", ".pt", ".pth", ".ckpt")
DOWNLOAD_TIMEOUT_SEC = 300
DOWNLOAD_RETRIES = 3

PI05_BASE_CHECKPOINT = "gs://openpi-assets/checkpoints/pi05_base"
"""Public address of the π0.5 base checkpoint.

Used when control.json does not specify one.
"""

LOCAL_CHECKPOINT_CACHE = Path("cache/pi05_base")
"""Local cache directory for the base checkpoint, relative to the current working
directory.

A `gs://` path cannot be mounted into the container directly, so an empty directory
is prepared and mounted instead, letting the openpi inside the container do the
download itself — the next training round then hits the cache instead of
re-downloading several GB.
"""


def resolve_checkpoint(configured: str) -> str:
    """Decide which checkpoint path gets mounted into the container.

    Anything starting with `gs://` is always replaced by the local cache directory
    (created empty if it does not exist); every other path is returned unchanged.
    Matches the branching in the old `miner.py`.
    """
    path = configured or PI05_BASE_CHECKPOINT
    if not path.startswith("gs://"):
        return path
    LOCAL_CHECKPOINT_CACHE.mkdir(parents=True, exist_ok=True)
    if any(LOCAL_CHECKPOINT_CACHE.iterdir()):
        logger.info("✅ Local base-model cache hit: %s", LOCAL_CHECKPOINT_CACHE)
    else:
        logger.info(
            "📥 Base-model cache is empty; the container will download it into: %s",
            LOCAL_CHECKPOINT_CACHE,
        )
    return str(LOCAL_CHECKPOINT_CACHE)


@dataclass
class TrainParams:
    """Hyperparameters for one training round.

    Comes from the `training` section of control.json.
    """

    epochs: int = 3
    batch_size: int = 4
    learning_rate: float = 1e-4
    lora_r: int = 32
    lora_alpha: int = 64
    max_episodes: int | None = None

    @classmethod
    def from_control(cls, training: dict[str, Any]) -> TrainParams:
        return cls(
            epochs=int(training.get("epochs", 3)),
            batch_size=int(training.get("batch_size", 4)),
            learning_rate=float(training.get("learning_rate", 1e-4)),
            lora_r=int(training.get("lora_r", 32)),
            lora_alpha=int(training.get("lora_alpha", 64)),
        )


@dataclass
class TrainOutcome:
    """The result of one training round."""

    metrics: dict[str, Any] = field(default_factory=dict)
    proof: dict[str, Any] = field(default_factory=dict)


def download_dataset(url: str, dest: str) -> str:
    """Download the dataset locally, retrying on failure. Returns the written path."""
    path = Path(dest)
    path.parent.mkdir(parents=True, exist_ok=True)

    last_error: Exception | None = None
    for attempt in range(1, DOWNLOAD_RETRIES + 1):
        try:
            request = build_request(url)
            with urlopen(request, DOWNLOAD_TIMEOUT_SEC) as response:
                path.write_bytes(response.read())
            logger.info("✅ Downloaded %s (%d bytes)", dest, path.stat().st_size)
            return dest
        except (OSError, urllib.error.URLError) as exc:
            last_error = exc
            wait = min(2**attempt, 30)
            logger.warning(
                "Download failed (attempt %d/%d): %s", attempt, DOWNLOAD_RETRIES, exc
            )
            if attempt < DOWNLOAD_RETRIES:
                time.sleep(wait)

    raise OSError(f"Dataset download failed for {url}: {last_error}")


def train_round(
    *,
    train_json_path: str,
    output_dir: str,
    checkpoint_path: str,
    params: TrainParams,
    hotkey: str,
    val_json_path: str | None = None,
    custom_train_script: str | None = None,
) -> TrainOutcome:
    """Load the data → run the container → assemble metrics and the training proof."""
    started = time.time()
    started_at = datetime.now(UTC).isoformat()

    train_samples = prepare_samples(
        load_episodes(train_json_path), max_episodes=params.max_episodes
    )
    train_count = len(train_samples)

    eval_samples: list[dict[str, Any]] | None = None
    if val_json_path and Path(val_json_path).is_file():
        eval_samples = prepare_samples(load_episodes(val_json_path))

    container_metrics, _container_proof = run_training(
        train_samples=train_samples,
        eval_samples=eval_samples,
        output_dir=output_dir,
        checkpoint_path=checkpoint_path,
        epochs=params.epochs,
        batch_size=params.batch_size,
        learning_rate=params.learning_rate,
        lora_r=params.lora_r,
        lora_alpha=params.lora_alpha,
        hotkey=hotkey,
        custom_train_script=custom_train_script,
    )

    duration = time.time() - started
    gpu_name, gpu_memory_gb = _gpu_stats()

    metrics = {
        "final_loss": container_metrics.get("final_loss", 0.0),
        "action_mse": container_metrics.get("action_mse", 0.0),
        "training_steps": container_metrics.get("train_steps", 0),
        "training_duration_seconds": duration,
        "gpu_memory_gb": round(gpu_memory_gb, 2),
        "train_samples": train_count,
    }

    proof = {
        "miner_uid": hotkey,
        "dataset_hash": file_hash(train_json_path),
        "adapter_hash": directory_hash(Path(output_dir) / "adapter"),
        "base_model_hash": (
            directory_hash(Path(checkpoint_path)) if checkpoint_path else ""
        ),
        "started_at": started_at,
        "ended_at": datetime.now(UTC).isoformat(),
        "loss_curve": container_metrics.get("loss_curve", []),
        "lr_schedule": container_metrics.get("lr_schedule", []),
        "total_steps": container_metrics.get("train_steps", 0),
        "total_samples": train_count,
        "gpu_device": gpu_name,
        "gpu_memory_peak_gb": round(gpu_memory_gb, 2),
        "client": f"openroboto-cli/{__version__}",
        "config_snapshot": {
            "model": "pi05",
            "config": "pi05_libero",
            "batch_size": params.batch_size,
            "learning_rate": params.learning_rate,
            "epochs": params.epochs,
        },
    }

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "metrics.json").write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    (out / "training_proof.json").write_text(
        json.dumps(proof, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    logger.info(
        "✅ Training finished in %.1fs | loss=%s | steps=%s",
        duration,
        metrics["final_loss"],
        proof["total_steps"],
    )
    return TrainOutcome(metrics=metrics, proof=proof)


def file_hash(path: str, prefix_len: int = HASH_PREFIX_LEN) -> str:
    """The first N characters of a single file's SHA256."""
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            digest.update(chunk)
    return digest.hexdigest()[:prefix_len]


def directory_hash(directory: Path, prefix_len: int = HASH_PREFIX_LEN) -> str:
    """The first N characters of the SHA256 over all weight files in a directory.

    Returns an empty string if the directory does not exist.

    The walk order is fixed (both directory names and file names are sorted);
    otherwise the same weights would hash differently on two machines.
    """
    if not directory.is_dir():
        return ""
    digest = hashlib.sha256()
    for root, dirs, files in os.walk(directory):
        dirs.sort()
        for name in sorted(files):
            if not name.endswith(WEIGHT_SUFFIXES):
                continue
            try:
                with open(Path(root) / name, "rb") as f:
                    for chunk in iter(lambda: f.read(8192), b""):
                        digest.update(chunk)
            except OSError:
                continue
    return digest.hexdigest()[:prefix_len]


def _gpu_stats() -> tuple[str, float]:
    """(GPU name, peak VRAM in GB).

    If the host has no torch installed, returns ("cpu", 0.0).
    """
    try:
        import torch
    except ImportError:
        return "cpu", 0.0
    if not torch.cuda.is_available():
        return "cpu", 0.0
    peak_gb = torch.cuda.max_memory_allocated() / 1024**3
    return str(torch.cuda.get_device_name(0)), peak_gb
