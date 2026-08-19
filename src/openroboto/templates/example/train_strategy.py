"""
Custom training strategy template

Mount this script into the container:
    -v /path/to/my_strategy.py:/data/scripts/my_strategy.py
    -e CUSTOM_TRAIN=/data/scripts/my_strategy.py

Interface: train(cfg, episodes, policy) → (metrics_dict, proof_dict)
"""

import time
from datetime import UTC, datetime

import torch


def train(cfg: dict, episodes: list, policy) -> tuple:
    """Training entry point: take the config, the training data and the policy
    object, run the training, and return the metrics and the proof.

    Args:
        cfg: config dict
            checkpoint_path, train_data, output_dir, epochs,
            batch_size, learning_rate, lora_r, lora_alpha, hotkey, ...
        episodes: training data list[dict]
        policy: openpi policy object (π₀.₅ checkpoint loaded)

    Returns:
        (metrics, proof): two dicts — the training metrics and the training proof.
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
        "started_at": datetime.now(UTC).isoformat(),
        "ended_at": datetime.now(UTC).isoformat(),
        "total_steps": steps,
        "gpu_device": _gpu_name(),
    }

    return metrics, proof


def _dummy_loss(policy, episode):
    """Replace with your actual loss function.

    Args:
        policy: openpi policy object
        episode: a single training episode

    Returns:
        loss tensor
    """
    return torch.tensor(1.0, requires_grad=True)


def _gpu_name() -> str:
    """Get the GPU device name via torch, or return 'cpu' if it is unavailable.

    Returns:
        the GPU device name, as a string
    """
    try:
        import torch
        if torch.cuda.is_available():
            return torch.cuda.get_device_name(0)
    except ImportError:
        pass
    return "cpu"
