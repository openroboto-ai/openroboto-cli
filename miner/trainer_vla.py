"""
miner/trainer_vla.py — π₀.₅ LIBERO VLA Training Launcher

Responsibilities:
  1. Load LIBERO episode dataset (JSON format)
  2. Call training_pipeline_vla.run_training() — Docker container training
  3. Collect metrics + Proof-of-Training

openpi training runs in an isolated container (openpi-runner/)
to avoid numpy version conflicts.
"""

import os
import time
import json
import logging
import hashlib
from typing import Optional
from datetime import datetime, timezone

import numpy as np

logger = logging.getLogger(__name__)


# ─── LIBERO Dataset Loading ──────────────────────────────────

def load_libero_episodes(json_path: str) -> list[dict]:
    """加载 LIBERO 回合数据集 / Load LIBERO episode dataset (JSON format).

    Expected format:
    [{"episode_id": "ep_001", "observation": {...}, "action": [...], ...}, ...]
    """
    from protocol.types import VLAEpisode

    logger.debug(f"[load_libero_episodes] Loading {json_path}")
    with open(json_path, "r") as f:
        raw = json.load(f)
    logger.debug(f"[load_libero_episodes] Parsed | type={type(raw).__name__} size={len(raw) if isinstance(raw, list) else 'dict'}")

    if isinstance(raw, dict):
        raw = raw.get("episodes", raw.get("data", [raw]))

    episodes = []
    for sample in raw:
        ep = VLAEpisode.from_dict(sample)
        errors = ep.validate()
        if errors:
            logger.warning(f"Skipping episode {sample.get('episode_id', '?')}: {errors}")
            continue
        episodes.append(ep.to_dict())

    logger.info(f"Loaded {len(episodes)} valid LIBERO episodes from {json_path}")
    return episodes


def prepare_libero_dataset(
    episodes: list[dict],
    max_episodes: Optional[int] = None,
) -> list[dict]:
    """将回合数据转换为 OpenPi 训练格式 / Convert to OpenPi LIBERO training format."""
    logger.debug(f"[prepare_libero_dataset] input: {len(episodes)} episodes, max={max_episodes}")
    if max_episodes:
        episodes = episodes[:max_episodes]

    processed = []
    for ep in episodes:
        obs = ep.get("observation", {})
        sample = {
            "observation/image": obs.get("image", []),
            "observation/wrist_image": obs.get("wrist_image", []),
            "observation/state": obs.get("state", []),
            "actions": ep.get("action", []),
            "prompt": ep.get("language_instruction", ""),
        }
        processed.append(sample)

    logger.info(f"Prepared {len(processed)} LIBERO samples for π₀.₅ training")
    return processed


# ─── Hash Utilities ──────────────────────────────────────────

def _compute_file_hash(path: str) -> str:
    """计算单个文件的 SHA256 哈希（前 16 位） / Compute SHA256 hash of a single file (first 16 chars)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(8192)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()[:16]


def _compute_dir_hash(dir_path: str, prefix_len: int = 16) -> str:
    """计算目录中模型权重文件的 SHA256 哈希 / Compute SHA256 hash of model weight files in a directory."""
    h = hashlib.sha256()
    for root, dirs, files in os.walk(dir_path):
        dirs.sort()
        for fname in sorted(files):
            if fname.endswith((".safetensors", ".bin", ".pt", ".pth", ".ckpt")):
                fpath = os.path.join(root, fname)
                try:
                    with open(fpath, "rb") as f:
                        while True:
                            chunk = f.read(8192)
                            if not chunk:
                                break
                            h.update(chunk)
                except Exception:
                    pass
    return h.hexdigest()[:prefix_len]


def _get_gpu_name() -> str:
    """获取当前 GPU 设备名称，若无 GPU 则返回 'cpu' / Get current GPU device name, or 'cpu' if none."""
    try:
        import torch
        if torch.cuda.is_available():
            return torch.cuda.get_device_name(0)
    except ImportError:
        pass
    return "cpu"


# ─── Main Training Entry ─────────────────────────────────────

def train_vla(
    checkpoint_path: Optional[str] = None,
    train_json_path: str = None,
    output_dir: str = "./output_vla",
    config=None,
    eval_json_path: Optional[str] = None,
    resume_adapter: Optional[str] = None,
    hf_token: Optional[str] = None,
    custom_train_script: Optional[str] = None,
) -> tuple:
    """π₀.₅ LIBERO 训练入口 / π₀.₅ LIBERO training entry point.

    Workflow:
      1. Load training/validation datasets
      2. Call training_pipeline_vla.run_training() — Docker container
      3. Generate metrics + Proof-of-Training

    openpi training runs in openpi-runner container to avoid numpy conflicts.
    """
    logger.debug(f"[train_vla] Start | checkpoint={checkpoint_path} train={train_json_path} output={output_dir}")
    logger.debug(f"[train_vla] custom_train_script={custom_train_script} eval={eval_json_path}")
    from miner.training_pipeline_vla import run_training

    start_time = time.time()
    started_at_iso = datetime.now(timezone.utc).isoformat()

    # 1. Load datasets
    logger.debug("[train_vla] Step 1: Load training dataset")
    train_episodes = load_libero_episodes(train_json_path)
    train_dataset = prepare_libero_dataset(
        train_episodes,
        max_episodes=getattr(config, "max_episodes", None) if config else None,
    )
    logger.debug(f"[train_vla] Training dataset ready: {len(train_dataset)} samples")

    eval_episodes = []
    eval_dataset = None
    if eval_json_path and os.path.exists(eval_json_path):
        logger.debug(f"[train_vla] Loading validation dataset: {eval_json_path}")
        eval_episodes = load_libero_episodes(eval_json_path)
        eval_dataset = prepare_libero_dataset(eval_episodes)
    else:
        logger.debug(f"[train_vla] No validation dataset (eval_json_path={eval_json_path})")

    # 2. Execute training (Docker container)
    logger.debug(f"[train_vla] Step 2: Call training_pipeline_vla.run_training")
    train_result, trained_policy = run_training(
        policy=None,  # Container loads its own
        train_dataset=train_dataset,
        eval_dataset=eval_dataset,
        output_dir=output_dir,
        config=config,
        custom_train_script=custom_train_script,
        checkpoint_path=checkpoint_path,
    )
    logger.debug(f"[train_vla] run_training returned | result_keys={list(train_result.keys()) if isinstance(train_result, dict) else 'not_dict'}")

    training_time = time.time() - start_time

    # 3. Collect metrics
    logger.debug("[train_vla] Step 3: Collect metrics + Proof-of-Training")
    final_loss = train_result.get("final_loss", 0.0)
    action_mse = train_result.get("action_mse", 0.0)

    gpu_mem_gb = 0.0
    try:
        import torch
        if torch.cuda.is_available():
            gpu_mem_gb = torch.cuda.max_memory_allocated() / (1024 ** 3)
            logger.debug(f"[train_vla] GPU peak memory: {gpu_mem_gb:.2f} GB")
    except ImportError:
        pass

    adapter_dir = os.path.join(output_dir, "adapter")
    adapter_exists = os.path.exists(adapter_dir)
    adapter_hash = _compute_dir_hash(adapter_dir) if adapter_exists else ""
    logger.debug(f"[train_vla] adapter_dir={adapter_dir} exists={adapter_exists} hash={adapter_hash}")

    metrics = {
        "final_loss": final_loss,
        "action_mse": action_mse,
        "training_steps": train_result.get("train_steps", 0),
        "training_duration_seconds": training_time,
        "gpu_memory_gb": round(gpu_mem_gb, 2),
        "train_samples": len(train_episodes),
    }

    # 4. Proof-of-Training
    logger.debug("[train_vla] Step 4: Generate Proof-of-Training")
    hotkey = config.hotkey if config and hasattr(config, "hotkey") else "unknown"
    checkpoint_hash = ""
    if checkpoint_path and os.path.exists(checkpoint_path):
        checkpoint_hash = _compute_dir_hash(checkpoint_path)
        logger.debug(f"[train_vla] checkpoint_hash={checkpoint_hash}")

    pot = {
        "miner_uid": hotkey,
        "dataset_hash": _compute_file_hash(train_json_path),
        "adapter_hash": adapter_hash,
        "base_model_hash": checkpoint_hash,
        "started_at": started_at_iso,
        "ended_at": datetime.now(timezone.utc).isoformat(),
        "loss_curve": train_result.get("loss_curve", []),
        "lr_schedule": train_result.get("lr_schedule", []),
        "total_steps": train_result.get("train_steps", 0),
        "total_samples": len(train_episodes),
        "gpu_device": _get_gpu_name(),
        "gpu_memory_peak_gb": round(gpu_mem_gb, 2),
        "config_snapshot": {
            "model": "pi05",
            "config": "pi05_libero",
            "batch_size": getattr(config, "batch_size", 4) if config else 4,
            "learning_rate": getattr(config, "learning_rate", 1e-4) if config else 1e-4,
            "epochs": getattr(config, "epochs", 3) if config else 3,
        },
    }

    # Save locally
    logger.debug(f"[train_vla] Saving metrics.json + training_proof.json → {output_dir}")
    os.makedirs(output_dir, exist_ok=True)
    with open(os.path.join(output_dir, "metrics.json"), "w") as f:
        json.dump(metrics, f, indent=2)
    with open(os.path.join(output_dir, "training_proof.json"), "w") as f:
        json.dump(pot, f, indent=2, ensure_ascii=False)

    logger.info(
        f"✅ π₀.₅ Training complete in {training_time:.1f}s | "
        f"loss={final_loss:.4f} | action_mse={action_mse:.4f} | "
        f"steps={pot['total_steps']}"
    )
    return metrics, pot
