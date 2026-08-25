#!/usr/bin/env python3
"""
RobotTrain — Simple Training Strategy

The smallest script that satisfies the `train(cfg, episodes, policy)` contract,
so the pipeline around it (miner → HF → chain → backend → validator) can be
exercised end to end. **It does not train, and it exports no checkpoint**: the
two lines you have to replace are marked below.

Why it exports nothing rather than something placeholder-shaped: the file it
would have to write to look like a checkpoint is `model.safetensors`, and
`openroboto check` would then pass on a directory full of random numbers. That
command is the one gate standing between a miner and a burn of TAO, and a green
verdict on noise is more expensive than a red one on an empty directory.

Usage — point `openroboto train` at this file, either way:

    openroboto train -s /path/to/simple_strategy.py

or set it once in miner.yaml:

    custom_train_script: /path/to/simple_strategy.py

`openroboto train` mounts the file into the training container for you:

    -v /path/to/simple_strategy.py:/data/scripts/simple_strategy.py
    -e CUSTOM_TRAIN=/data/scripts/simple_strategy.py

You do not run docker yourself; the two lines above are shown so that a failing
run is diagnosable from `docker ps`.
"""

import logging
import os
import time
from datetime import UTC, datetime

logger = logging.getLogger(__name__)


def train(cfg: dict, episodes: list, policy=None) -> tuple:
    """Simple training flow: a fake loss loop, and the export slot left empty.

    Args:
        cfg: config dict (checkpoint_path, epochs, batch_size, lr, lora_r, lora_alpha, hotkey, output_dir)
        episodes: training data list
        policy: openpi policy object (or None for simple mode)

    Returns:
        (metrics_dict, proof_dict): the training metrics dict and the training
        proof dict.
    """
    start = time.time()
    output_dir = cfg["output_dir"]
    epochs = cfg.get("epochs", 3)

    logger.info(f"[simple_strategy] Starting | epochs={epochs} episodes={len(episodes)}")

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

    # ── Export the checkpoint ───────────────────────────
    #
    # ↓↓↓ THIS IS THE STEP YOU HAVE TO WRITE ↓↓↓
    #
    # `output_dir` **is** the checkpoint root. `openroboto submit` uploads this
    # directory verbatim as the Hugging Face repository root, and the evaluator
    # looks for the weights at the top of it, descending only a couple of
    # levels. Two rules follow, and both of them cost a burn when broken:
    #
    #   1. Write the weights at the top of `output_dir`. If your trainer
    #      insists on its own layout -- the LingBot exporter writes
    #      `checkpoints/global_step_N/hf_ckpt/`, three levels down -- move or
    #      copy that directory's contents up before you finish here.
    #   2. Write the **full** checkpoint, not a LoRA adapter. Nothing merges an
    #      adapter: not this package (there is no `openroboto merge`, and the
    #      model libraries do not fit in the same interpreter as bittensor) and
    #      not the evaluator. A bare adapter is rejected before a GPU is
    #      allocated.
    #
    # Whatever your trainer's HF-format export is, point it at `output_dir`.
    #
    # ↑↑↑ THIS IS THE STEP YOU HAVE TO WRITE ↑↑↑
    os.makedirs(output_dir, exist_ok=True)
    logger.warning(
        "[simple_strategy] ⚠️  no checkpoint exported — this strategy only "
        "exercises the pipeline. `openroboto check` will say so; replace the "
        "export step above before you submit."
    )

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
        "started_at": datetime.now(UTC).isoformat(),
        "ended_at": datetime.now(UTC).isoformat(),
        "total_steps": global_step,
        "strategy": "simple",
        "gpu_device": _get_gpu_name(),
    }

    logger.info(f"[simple_strategy] ✅ Done in {duration:.1f}s | loss={final_loss:.4f}")
    return metrics, proof


def _get_gpu_name() -> str:
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
