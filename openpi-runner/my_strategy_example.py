"""
Custom training strategy template

Mount this script into the container:
    -v /path/to/my_strategy.py:/data/scripts/my_strategy.py
    -e CUSTOM_TRAIN=/data/scripts/my_strategy.py

Interface: train(cfg, episodes, policy) → (metrics_dict, proof_dict)
"""

import time
import torch
from datetime import datetime, timezone


def train(cfg: dict, episodes: list, policy) -> tuple:
    """训练入口函数：接收配置、训练数据和策略对象，执行训练并返回指标与证明。

    Training entry function.

    Args:
        cfg: config dict / 配置字典
            checkpoint_path, train_data, output_dir, epochs,
            batch_size, learning_rate, lora_r, lora_alpha, hotkey, ...
        episodes: training data list[dict] / 训练数据列表
        policy: openpi policy object (π₀.₅ checkpoint loaded) / openpi 策略对象

    Returns:
        (metrics, proof) two dicts / 训练指标和证明字典
    """

    start = time.time()

    # ═══════════════════════════════════════════
    # ↓↓↓ Write your training logic here ↓↓↓
    # ═══════════════════════════════════════════

    optimizer = torch.optim.Adam(
        policy.parameters(), lr=cfg["learning_rate"]
    )

    steps = 0
    loss_curve = []

    for epoch in range(cfg["epochs"]):
        for ep in episodes:
            # TODO: Replace with real forward pass
            loss = _dummy_loss(policy, ep)

            optimizer.zero_grad()
            loss.backward()
            optimizer.step()

            steps += 1
            if steps % 10 == 0:
                loss_curve.append({
                    "step": steps,
                    "loss": round(loss.item(), 6),
                })

    # ↑↑↑ Write your training logic here ↑↑↑
    # ═══════════════════════════════════════════

    # Save model adapter (required)
    adapter_dir = f"{cfg['output_dir']}/adapter"
    if hasattr(policy, "save_pretrained"):
        policy.save_pretrained(adapter_dir)

    # Build return value
    metrics = {
        "final_loss": loss_curve[-1]["loss"] if loss_curve else 0.5,
        "training_steps": steps,
        "training_duration_seconds": time.time() - start,
        "loss_curve": loss_curve,
    }

    proof = {
        "miner_uid": cfg["hotkey"],
        "started_at": datetime.now(timezone.utc).isoformat(),
        "ended_at": datetime.now(timezone.utc).isoformat(),
        "total_steps": steps,
        "gpu_device": _gpu_name(),
    }

    return metrics, proof


def _dummy_loss(policy, episode):
    """替换为实际的损失函数。

    Replace with your actual loss function.

    Args:
        policy: openpi 策略对象
        episode: 单个训练片段

    Returns:
        loss tensor
    """
    return torch.tensor(1.0, requires_grad=True)


def _gpu_name() -> str:
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
