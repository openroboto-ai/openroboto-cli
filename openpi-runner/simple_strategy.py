#!/usr/bin/env python3
"""
RobotTrain — Simple Training Strategy

A minimal training script that generates valid LoRA adapter files
for testing the full pipeline (miner → HF → chain → backend → validator).

Usage (miner sets CUSTOM_TRAIN_SCRIPT env var):
    CUSTOM_TRAIN_SCRIPT=/path/to/simple_strategy.py python miner.py --config config.yaml

This script is mounted into the Docker container:
    -v /path/to/simple_strategy.py:/data/scripts/simple_strategy.py
    -e CUSTOM_TRAIN=/data/scripts/simple_strategy.py
"""

import os
import sys
import json
import time
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def train(cfg: dict, episodes: list, policy=None) -> tuple:
    """简单训练流程：模拟损失循环 + 生成有效的 LoRA 适配器文件。

    Simple training: fake loss loop + generate valid LoRA adapter files.

    Args:
        cfg: config dict (checkpoint_path, epochs, batch_size, lr, lora_r, lora_alpha, hotkey, output_dir)
            / 配置字典
        episodes: training data list / 训练数据列表
        policy: openpi policy object (or None for simple mode) / openpi 策略对象（或 None）

    Returns:
        (metrics_dict, proof_dict) / 训练指标和证明字典
    """
    start = time.time()
    output_dir = cfg["output_dir"]
    r = cfg.get("lora_r", 32)
    alpha = cfg.get("lora_alpha", 64)
    epochs = cfg.get("epochs", 3)

    logger.info(f"[simple_strategy] Starting | epochs={epochs} lora_r={r} episodes={len(episodes)}")

    # ── Fake training loop ──────────────────────────────
    loss_curve = []
    global_step = 0

    for epoch in range(epochs):
        for ep in episodes:
            # Fake decreasing loss
            loss = max(0.01, 1.0 / (1 + global_step * 0.01))
            global_step += 1
            if global_step % 10 == 0:
                loss_curve.append({"step": global_step, "loss": round(loss, 6)})
                logger.info(f"  Step {global_step}: loss={loss:.4f}")
        logger.info(f"  Epoch {epoch} done")

    # ── Generate LoRA adapter files ─────────────────────
    adapter_dir = os.path.join(output_dir, "adapter")
    os.makedirs(adapter_dir, exist_ok=True)
    _save_lora_adapter(adapter_dir, cfg)
    logger.info(f"[simple_strategy] 💾 Adapter saved to {adapter_dir}")

    # ── Metrics ─────────────────────────────────────────
    final_loss = loss_curve[-1]["loss"] if loss_curve else 0.5
    duration = time.time() - start

    metrics = {
        "final_loss": final_loss,
        "training_steps": global_step,
        "training_duration_seconds": duration,
        "loss_curve": loss_curve,
    }

    proof = {
        "miner_uid": cfg.get("hotkey", "unknown"),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "ended_at": datetime.now(timezone.utc).isoformat(),
        "total_steps": global_step,
        "strategy": "simple",
        "gpu_device": _get_gpu_name(),
    }

    logger.info(f"[simple_strategy] ✅ Done in {duration:.1f}s | loss={final_loss:.4f}")
    return metrics, proof


def _save_lora_adapter(adapter_dir: str, cfg: dict):
    """生成结构正确但内容为模拟的 LoRA 适配器文件。

    Generate structurally valid LoRA adapter files (fake but parseable).

    Args:
        adapter_dir: 适配器输出目录
        cfg: 配置字典，包含 lora_r、lora_alpha、checkpoint_path 等
    """
    r = cfg.get("lora_r", 32)
    alpha = cfg.get("lora_alpha", 64)

    try:
        import numpy as np
        try:
            import safetensors.numpy as st
            has_safetensors = True
        except ImportError:
            has_safetensors = False

        tensors = {}
        for module in ["q_proj", "k_proj", "v_proj", "o_proj"]:
            tensors[f"base_model.model.transformer.layers.0.self_attn.{module}.lora_A.weight"] = \
                np.random.randn(2048, r).astype(np.float32) * 0.01
            tensors[f"base_model.model.transformer.layers.0.self_attn.{module}.lora_B.weight"] = \
                np.random.randn(r, 2048).astype(np.float32) * 0.01

        if has_safetensors:
            st.save_file(tensors, os.path.join(adapter_dir, "adapter_model.safetensors"))
        else:
            np.savez(os.path.join(adapter_dir, "adapter_model.npz"), **tensors)
    except ImportError:
        # Fallback: empty placeholder
        logger.warning("[simple_strategy] No numpy/safetensors, creating empty adapter files")
        with open(os.path.join(adapter_dir, "adapter_model.placeholder"), "w") as f:
            f.write("")

    # adapter_config.json
    config = {
        "alpha_pattern": {},
        "auto_mapping": None,
        "base_model_name_or_path": cfg.get("checkpoint_path", "openpi/pi05_base"),
        "bias": "none",
        "inference_mode": True,
        "init_lora_weights": True,
        "lora_alpha": alpha,
        "lora_dropout": 0.0,
        "peft_type": "LORA",
        "r": r,
        "target_modules": ["q_proj", "k_proj", "v_proj", "o_proj"],
        "task_type": None,
        "use_rslora": False,
    }
    with open(os.path.join(adapter_dir, "adapter_config.json"), "w") as f:
        json.dump(config, f, indent=2)


def _get_gpu_name() -> str:
    """获取 GPU 设备名称，如不可用则返回 'cpu'。

    Get GPU device name via torch, or return 'cpu' if unavailable.

    Returns:
        GPU 设备名称字符串
    """
    try:
        import torch
        if torch.cuda.is_available():
            return torch.cuda.get_device_name(0)
    except ImportError:
        pass
    return "cpu"
