"""
Custom training strategy template

Mount this script into the container:
    -v /path/to/my_strategy.py:/data/scripts/my_strategy.py
    -e CUSTOM_TRAIN=/data/scripts/my_strategy.py

Interface: train(cfg, episodes, policy) → (metrics_dict, proof_dict)

Two blocks are marked below and both are yours to fill in: the training loop,
and the **export**. The export is the one that decides whether the round is
worth anything -- `cfg["output_dir"]` is uploaded verbatim as the Hugging Face
repository root, so what you leave there is exactly what gets evaluated.
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

    # ═══════════════════════════════════════════
    # ↓↓↓ Export the checkpoint (required) ↓↓↓
    # ═══════════════════════════════════════════
    #
    # `cfg["output_dir"]` **is** the checkpoint root: `openroboto submit`
    # uploads this directory verbatim as the Hugging Face repository root, and
    # the evaluator looks for the weights at the top of it, descending only a
    # couple of levels. So:
    #
    #   * export at the top of `output_dir`, never into a subdirectory. The
    #     LingBot exporter writes `checkpoints/global_step_N/hf_ckpt/` -- three
    #     levels down, one too many -- so if you use it, move the contents up
    #     here;
    #   * export the **full** checkpoint, not a LoRA adapter. Nothing merges an
    #     adapter: not this package (there is no `openroboto merge`) and not the
    #     evaluator, which rejects a bare adapter before allocating a GPU.
    #
    # The call below is openpi's; substitute your own trainer's HF-format
    # export, pointed at the same directory.
    if hasattr(policy, "save_pretrained"):
        policy.save_pretrained(cfg["output_dir"])
    #
    # ↑↑↑ Export the checkpoint (required) ↑↑↑
    # ═══════════════════════════════════════════

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
