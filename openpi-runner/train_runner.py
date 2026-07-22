"""
π₀.₅ OpenPi Runner — Isolated training container entry

Invoked by main process via docker run, receives env var parameters,
Reads/writes mounted data directories.

Usage:
    docker run --gpus all \
      -v /host/data:/data/input \
      -v /host/output:/data/output \
      -v ~/.cache/openpi:/data/cache \
      -e CHECKPOINT_PATH=/data/cache/pi05_base \
      -e TRAIN_DATA=/data/input/train.json \
      -e OUTPUT_DIR=/data/output \
      -e EPOCHS=3 \
      -e BATCH_SIZE=4 \
      -e LR=1e-4 \
      robot-train-openpi
"""

import os
import sys
import json
import time
import logging
from typing import Optional
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


# ─── Config from env ──────────────────────────────────────

def get_config() -> dict:
    """从环境变量读取训练配置。

    Read training config from environment variables.

    Returns:
        配置字典，包含 checkpoint_path、train_data、epochs、batch_size 等。
    """
    return {
        "checkpoint_path": os.getenv("CHECKPOINT_PATH", "/data/cache/pi05_base"),
        "train_data": os.getenv("TRAIN_DATA", "/data/input/train.json"),
        "val_data": os.getenv("VAL_DATA", ""),
        "output_dir": os.getenv("OUTPUT_DIR", "/data/output"),
        "epochs": int(os.getenv("EPOCHS", "3")),
        "batch_size": int(os.getenv("BATCH_SIZE", "4")),
        "learning_rate": float(os.getenv("LR", "1e-4")),
        "warmup_ratio": float(os.getenv("WARMUP_RATIO", "0.05")),
        "lora_r": int(os.getenv("LORA_R", "32")),
        "lora_alpha": int(os.getenv("LORA_ALPHA", "64")),
        "hotkey": os.getenv("HOTKEY", "unknown"),
    }


# ─── Training ─────────────────────────────────────────────

def run_training(cfg: dict) -> tuple:
    """执行 π₀.₅ LIBERO 训练，支持自定义脚本或默认流程。

    Execute π₀.₅ LIBERO training, returns (metrics, proof).
    Uses custom training script if CUSTOM_TRAIN env var is set, otherwise runs default.

    Args:
        cfg: configuration dictionary from get_config()

    Returns:
        (metrics, proof) 两个字典
    """

    custom_train = os.getenv("CUSTOM_TRAIN", "")
    if custom_train and os.path.exists(custom_train):
        logger.info(f"🔧 Using custom training script: {custom_train}")
        return _run_custom(cfg, custom_train)
    return _run_default(cfg)


def _run_custom(cfg: dict, custom_script: str) -> tuple:
    """加载并执行外部挂载的自定义训练脚本。

    Load and execute externally mounted custom training script.

    Script interface contract:
        def train(cfg: dict, episodes: list, policy) -> tuple:
            # cfg: config dict read from env vars / 从环境变量读取的配置字典
            # episodes: loaded training data list / 已加载的训练数据列表
            # policy: openpi policy (checkpoint already downloaded) / openpi 策略对象
            # return: (metrics_dict, proof_dict)

    Args:
        cfg: configuration dictionary / 配置字典
        custom_script: path to custom training script / 自定义训练脚本路径

    Returns:
        (metrics, proof) 两个字典
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location("custom_train", custom_script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    if not hasattr(mod, "train"):
        raise ValueError(f"{custom_script} Missing train(cfg, episodes, policy) entry function")

    # Prepare common dependencies
    from openpi.shared import download
    from openpi.training import config as _config
    from openpi.policies import policy_config

    cp = cfg["checkpoint_path"]
    if cp.startswith("gs://"):
        cp = download.maybe_download(cp)
    elif not os.path.exists(cp):
        cp = download.maybe_download("gs://openpi-assets/checkpoints/pi05_base")

    logger.info(f"📦 π₀.₅ checkpoint: {cp}")
    train_cfg = _config.get_config("pi05_libero")
    episodes = _load_episodes(cfg["train_data"])
    logger.info(f"📊 Loaded {len(episodes)} episodes")
    norm_stats = _compute_norm_stats(episodes)
    policy = policy_config.create_trained_policy(train_cfg, cp, norm_stats=norm_stats)

    metrics, proof = mod.train(cfg, episodes, policy)

    output_dir = cfg["output_dir"]
    os.makedirs(output_dir, exist_ok=True)
    with open(os.path.join(output_dir, "metrics.json"), "w") as f:
        json.dump(metrics, f, indent=2)
    with open(os.path.join(output_dir, "proof.json"), "w") as f:
        json.dump(proof, f, indent=2)

    return metrics, proof


def _run_default(cfg: dict) -> tuple:
    """默认训练流程：使用模拟损失曲线和伪 LoRA 权重进行管道测试。

    Simple training loop with fake LoRA weights for pipeline testing.

    Args:
        cfg: configuration dictionary / 配置字典

    Returns:
        (metrics, proof) 包含训练指标和证明的字典
    """

    # Download checkpoint if needed
    cp = cfg["checkpoint_path"]
    logger.info(f"📦 π₀.₅ checkpoint: {cp}")

    # Load training data
    episodes = _load_episodes(cfg["train_data"])
    logger.info(f"📊 Loaded {len(episodes)} episodes")
    norm_stats = _compute_norm_stats(episodes)
    logger.info(f"📈 Norm stats computed")

    # Simple training loop with fake loss
    start_time = time.time()
    loss_curve = []
    global_step = 0

    for epoch in range(cfg["epochs"]):
        for i, ep in enumerate(episodes):
            # Fake training: loss decreases over steps
            loss = max(0.01, 1.0 / (1 + global_step * 0.01))
            global_step += 1

            if global_step % 10 == 0:
                loss_curve.append({"step": global_step, "loss": round(loss, 6)})
                logger.info(f"   Step {global_step}: loss={loss:.4f}")

        logger.info(f"  Epoch {epoch} done")

    # Save LoRA adapter files (fake but structurally correct)
    output_dir = cfg["output_dir"]
    os.makedirs(output_dir, exist_ok=True)
    adapter_dir = os.path.join(output_dir, "adapter")
    os.makedirs(adapter_dir, exist_ok=True)
    _save_lora_adapter(adapter_dir, cfg)
    logger.info(f"💾 LoRA adapter saved to {adapter_dir}")

    # Save norm stats — openpi requires assets/physical-intelligence/libero/norm_stats.json
    _save_norm_stats(output_dir, norm_stats)
    logger.info(f"📊 Norm stats saved")

    # Metrics
    final_loss = loss_curve[-1]["loss"] if loss_curve else 0.5
    duration = time.time() - start_time

    metrics = {
        "final_loss": final_loss,
        "training_steps": global_step,
        "training_duration_seconds": duration,
        "loss_curve": loss_curve,
    }

    with open(os.path.join(output_dir, "metrics.json"), "w") as f:
        json.dump(metrics, f, indent=2)

    # Proof
    proof = {
        "miner_uid": cfg["hotkey"],
        "started_at": datetime.now(timezone.utc).isoformat(),
        "ended_at": datetime.now(timezone.utc).isoformat(),
        "total_steps": global_step,
        "gpu_device": _get_gpu_name(),
    }

    with open(os.path.join(output_dir, "proof.json"), "w") as f:
        json.dump(proof, f, indent=2)

    logger.info(f"✅ Training complete in {duration:.1f}s | loss={final_loss:.4f}")
    return metrics, proof


def _save_lora_adapter(adapter_dir: str, cfg: dict):
    """生成结构正确但内容为模拟的 LoRA 适配器文件。

    Generate fake but structurally valid LoRA adapter files.

    Creates:
    - adapter_config.json (LoRA metadata) / LoRA 元数据配置
    - adapter_model.safetensors (fake but valid binary weights) / 模拟权重文件

    Args:
        adapter_dir: 适配器输出目录
        cfg: 配置字典，包含 lora_r、lora_alpha、checkpoint_path 等
    """
    import numpy as np
    try:
        import safetensors.numpy as st
        has_safetensors = True
    except ImportError:
        has_safetensors = False

    r = cfg["lora_r"]
    alpha = cfg["lora_alpha"]

    # Fake LoRA weights for target modules
    tensors = {}
    for module in ["q_proj", "k_proj", "v_proj", "o_proj"]:
        # A matrix: (hidden_dim, r)
        tensors[f"base_model.model.transformer.layers.0.self_attn.{module}.lora_A.weight"] = \
            np.random.randn(2048, r).astype(np.float32) * 0.01
        # B matrix: (r, hidden_dim)
        tensors[f"base_model.model.transformer.layers.0.self_attn.{module}.lora_B.weight"] = \
            np.random.randn(r, 2048).astype(np.float32) * 0.01

    # Save safetensors
    if has_safetensors:
        st.save_file(tensors, os.path.join(adapter_dir, "adapter_model.safetensors"))
    else:
        # Fallback: save as numpy .npz (push_hf.py will convert)
        np.savez(os.path.join(adapter_dir, "adapter_model.npz"), **tensors)

    # Save adapter config
    config = {
        "alpha_pattern": {},
        "auto_mapping": None,
        "base_model_name_or_path": cfg.get("checkpoint_path", "openpi/pi05_base"),
        "bias": "none",
        "fan_in_fan_out": False,
        "inference_mode": True,
        "init_lora_weights": True,
        "layers_pattern": None,
        "layers_to_transform": None,
        "loftq_config": {},
        "lora_alpha": alpha,
        "lora_dropout": 0.0,
        "megatron_config": None,
        "megatron_core": "megatron.core",
        "modules_to_save": [],
        "peft_type": "LORA",
        "r": r,
        "rank_pattern": {},
        "revision": None,
        "target_modules": ["q_proj", "k_proj", "v_proj", "o_proj"],
        "task_type": None,
        "use_rslora": False,
    }
    with open(os.path.join(adapter_dir, "adapter_config.json"), "w") as f:
        json.dump(config, f, indent=2)


def _load_episodes(path: str) -> list:
    """从 JSON 文件加载训练片段（episodes）数据。

    Load episode JSON data from the given path.

    Args:
        path: JSON 文件路径

    Returns:
        episodes 列表
    """
    with open(path, "r") as f:
        data = json.load(f)
    if isinstance(data, dict):
        data = data.get("episodes", data.get("data", [data]))
    return data


def _compute_norm_stats(episodes: list) -> dict:
    """从训练数据中计算 openpi 兼容的归一化统计信息。

    Compute norm stats from training data, matching openpi expected format.
    Returns a dict[str, NormStats] compatible with openpi's Normalize transform.

    Args:
        episodes: 训练片段列表 / Episode list.
    Returns:
        {"state": NormStats(...), "actions": NormStats(...)}
    """
    import numpy as np

    if not episodes:
        return {}

    all_actions = []
    all_states = []
    for ep in episodes:
        # Action
        actions = ep.get("actions", ep.get("action", []))
        if isinstance(actions, list):
            all_actions.extend(actions)
        # State
        keys = list(ep.keys())
        is_flat = any("/" in k for k in keys)
        if is_flat:
            states_raw = ep.get("observation/state", [])
        else:
            obs = ep.get("observation", {})
            states_raw = obs.get("state", []) if isinstance(obs, dict) else []

        decoded = []
        for s in states_raw:
            if isinstance(s, dict) and "values" in s:
                decoded.append({"type": s.get("type", ""), "values": s["values"]})
            elif isinstance(s, list):
                decoded.append({"type": "", "values": s})
            elif isinstance(s, (int, float)):
                decoded.append({"type": "", "values": [float(s)]})

        # Merge consecutive states of different types (same as _decode_episodes):
        # e.g. arm_joint_angles (dim=7) + gripper_width (dim=1) → combined (dim=8)
        i = 0
        while i < len(decoded):
            combined = list(decoded[i]["values"])
            i += 1
            if i < len(decoded):
                curr_type = decoded[i - 1].get("type", "")
                next_type = decoded[i].get("type", "")
                if next_type and next_type != curr_type:
                    combined.extend(list(decoded[i]["values"]))
                    i += 1
                elif not next_type and not curr_type:
                    combined.extend(list(decoded[i]["values"]))
                    i += 1
            all_states.append(combined)

    def _pad(values, dim):
        """Pad/trim all vectors to uniform dimension."""
        result = []
        for v in values:
            if len(v) < dim:
                result.append(list(v) + [0.0] * (dim - len(v)))
            else:
                result.append(list(v[:dim]))
        return result

    def _make_norm_stats(values, fallback_dim=8):
        """Compute mean, std, q01, q99 and return a NormStats object.
        计算均值、标准差、1%和99%分位数并返回 NormStats 对象。"""
        from openpi.shared.normalize import NormStats

        if not values:
            fb = np.zeros(fallback_dim, dtype=np.float32)
            ones = np.ones(fallback_dim, dtype=np.float32)
            return NormStats(mean=fb, std=ones, q01=fb, q99=fb)
        # Ensure all vectors have the same dimension
        dim = max(len(v) for v in values)
        if dim == 0:
            fb = np.zeros(fallback_dim, dtype=np.float32)
            ones = np.ones(fallback_dim, dtype=np.float32)
            return NormStats(mean=fb, std=ones, q01=fb, q99=fb)
        values = _pad(values, dim)
        arr = np.array(values, dtype=np.float64)
        if arr.ndim == 1:
            arr = arr.reshape(-1, 1)
        mean = np.mean(arr, axis=0).astype(np.float32)
        std = np.std(arr, axis=0).astype(np.float32)
        std = np.where(std > 1e-8, std, np.ones_like(std))
        q01 = np.percentile(arr, 1, axis=0).astype(np.float32)
        q99 = np.percentile(arr, 99, axis=0).astype(np.float32)
        return NormStats(mean=mean, std=std, q01=q01, q99=q99)

    return {
        "state": _make_norm_stats(all_states),
        "actions": _make_norm_stats(all_actions),
    }


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


# ─── Main ─────────────────────────────────────────────────

def main():
    """程序入口：初始化日志、读取配置、执行训练并输出结果。

    Main entry point: init logging, read config, run training, print JSON result.
    """
    logging.basicConfig(level=logging.INFO, format="%(asctime)s,%(msecs)03d [%(name)s] [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    cfg = get_config()

    logger.info(f"🚀 π₀.₅ OpenPi Runner")
    logger.info(f"   Checkpoint: {cfg['checkpoint_path']}")
    logger.info(f"   Train data: {cfg['train_data']}")
    logger.info(f"   Output:     {cfg['output_dir']}")
    logger.info(f"   Epochs:     {cfg['epochs']} | BS: {cfg['batch_size']} | LR: {cfg['learning_rate']}")

    metrics, proof = run_training(cfg)

    # Print final result as JSON for host parsing
    print("---RESULT---")
    print(json.dumps({"metrics": metrics, "proof": proof}, indent=2))


if __name__ == "__main__":
    main()
