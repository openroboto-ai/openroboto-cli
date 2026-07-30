# Custom Training Strategy — Usage Guide

## Overview

Custom training scripts are injected into the container through a **volume mount**, so you can replace the training logic without rebuilding the Docker image.

```
host (miner)                          container (openpi-runner)
┌─────────────────┐              ┌─────────────────────────┐
│                 │  -v mount    │                         │
│ my_strategy.py  │ ─────────►  │ /data/scripts/my_       │
│                 │              │   strategy.py           │
│                 │  -e env      │                         │
│                 │ ─────────►  │ CUSTOM_TRAIN=...        │
│                 │              │                         │
│ train_vla()     │  docker run  │ train_runner.py         │
│   ├─ custom_    │ ─────────►  │   ├─ detect CUSTOM_TRAIN│
│   │  train_     │              │   ├─ call _run_custom() │
│   │  script=... │              │   │   └─ train(cfg,…)   │
│                 │              │   └─ write metrics.json │
│                 │ ◄──────────  │     proof.json          │
│ read results ←── stdout + files│                         │
└─────────────────┘              └─────────────────────────┘
```

## Script interface

Your script must define a `train` function:

```python
def train(cfg: dict, episodes: list, policy) -> tuple:
    """
    Args:
        cfg: config dict with the following keys:
            - checkpoint_path: base model path (may already be resolved to a local path)
            - train_data: training data path
            - val_data: validation data path (optional)
            - output_dir: output directory (already mounted)
            - epochs: number of training epochs
            - batch_size: batch size
            - learning_rate: learning rate
            - warmup_ratio: warmup ratio
            - lora_r: LoRA rank
            - lora_alpha: LoRA alpha
            - hotkey: miner hotkey

        episodes: pre-loaded training data (list[dict])
        policy: openpi policy object with the π₀.₅ checkpoint already loaded

    Returns:
        (metrics, proof) — two dicts:
        - metrics: final_loss, training_steps, loss_curve, etc.
        - proof: miner_uid, gpu_device, started_at, ended_at, etc.
    """
    ...
```

## Quick examples

### Example 1: minimal runnable script

```python
import time
from datetime import datetime, timezone

def train(cfg, episodes, policy):
    """Minimal runnable training script — iterate all episodes with a dummy loss."""

    start = time.time()
    steps = 0
    loss_curve = []

    for epoch in range(cfg["epochs"]):
        for ep in episodes:
            steps += 1
            # ← replace with your training logic
            loss = 1.0 / (1 + steps * 0.01)
            if steps % 10 == 0:
                loss_curve.append({"step": steps, "loss": round(loss, 6)})

    # Save the model
    adapter_dir = f"{cfg['output_dir']}/adapter"
    if hasattr(policy, "save_pretrained"):
        policy.save_pretrained(adapter_dir)

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


def _gpu_name():
    try:
        import torch
        if torch.cuda.is_available():
            return torch.cuda.get_device_name(0)
    except ImportError:
        pass
    return "cpu"
```

### Example 2: using the native openpi training API

```python
import time
import torch
from datetime import datetime, timezone
from openpi.training import data_loader as _data_loader

def train(cfg, episodes, policy):
    """Use the native openpi data loader and training loop."""

    start = time.time()
    optimizer = torch.optim.Adam(policy.parameters(), lr=cfg["learning_rate"])
    steps = 0
    loss_curve = []

    for epoch in range(cfg["epochs"]):
        loader = _data_loader.create_dataloader(
            episodes,
            batch_size=cfg["batch_size"],
        )
        for batch in loader:
            optimizer.zero_grad()
            outputs = policy(batch)
            loss = outputs.loss
            loss.backward()
            optimizer.step()
            steps += 1

            if steps % 10 == 0:
                loss_curve.append({
                    "step": steps,
                    "loss": round(loss.item(), 6),
                })

    # Save the adapter
    adapter_dir = f"{cfg['output_dir']}/adapter"
    policy.save_pretrained(adapter_dir)

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


def _gpu_name():
    try:
        import torch
        if torch.cuda.is_available():
            return torch.cuda.get_device_name(0)
    except ImportError:
        pass
    return "cpu"
```

### Example 3: custom optimization strategy

```python
import time
import torch
from datetime import datetime, timezone

def train(cfg, episodes, policy):
    """Example: custom scheduler + gradient clipping."""

    start = time.time()

    optimizer = torch.optim.AdamW(
        policy.parameters(),
        lr=cfg["learning_rate"],
        weight_decay=1e-2,
    )

    total_steps = cfg["epochs"] * len(episodes)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(
        optimizer, T_max=total_steps,
    )

    steps = 0
    loss_curve = []
    max_grad_norm = cfg.get("max_grad_norm", 1.0)

    for epoch in range(cfg["epochs"]):
        for ep in episodes:
            # ← your forward pass
            loss = compute_loss(policy, ep)

            loss.backward()
            torch.nn.utils.clip_grad_norm_(policy.parameters(), max_grad_norm)
            optimizer.step()
            optimizer.zero_grad()

            scheduler.step()
            steps += 1

            if steps % 10 == 0:
                loss_curve.append({
                    "step": steps,
                    "loss": round(loss.item(), 6),
                    "lr": scheduler.get_last_lr()[0],
                })

    # Save
    adapter_dir = f"{cfg['output_dir']}/adapter"
    policy.save_pretrained(adapter_dir)

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


def compute_loss(policy, episode):
    """← your loss function logic."""
    return ...


def _gpu_name():
    try:
        import torch
        if torch.cuda.is_available():
            return torch.cuda.get_device_name(0)
    except ImportError:
        pass
    return "cpu"
```

## How to use it

### Option 1: pass it in miner.py

```python
from miner.trainer_vla import train_vla

train_vla(
    checkpoint_path=...,
    train_json_path=...,
    output_dir="/tmp/output_vla",
    config=train_cfg,
    hf_token=hf_token,
    custom_train_script="/path/to/my_strategy.py",  # ← add this line
)
```

### Option 2: through config.yaml

Add the path to your miner config:

```yaml
training:
  custom_train_script: /path/to/my_strategy.py
```

Then read the setting in `trainer_vla.py` or `training_pipeline_vla.py` and pass it through.

### Option 3: direct docker run for testing

Skip the miner and test the script directly:

```bash
docker run --rm --gpus all \
  -v /host/data:/data/input \
  -v /host/output:/data/output \
  -v /path/to/my_strategy.py:/data/scripts/my_strategy.py \
  -e CHECKPOINT_PATH=/data/cache/pi05_base \
  -e TRAIN_DATA=/data/input/train.json \
  -e OUTPUT_DIR=/data/output \
  -e EPOCHS=3 \
  -e BATCH_SIZE=4 \
  -e LR=1e-4 \
  -e CUSTOM_TRAIN=/data/scripts/my_strategy.py \
  robot-train-openpi
```

## Directory layout

```
my_training/
├── my_strategy.py          # custom training script
├── utils.py                # custom modules (optional)
└── configs/
    └── my_config.yaml      # custom config (optional)

# Mount the whole directory:
docker run -v /path/to/my_training:/data/scripts ...
# then use relative paths inside the script
```

## Notes

1. **You must define a `train(cfg, episodes, policy)` function**, otherwise the container exits with an error.
2. **You must return the `(metrics, proof)` dict pair** in the same format as the default pipeline.
3. **Model save directory** must be `cfg['output_dir']/adapter` — that is where the training pipeline collects your output from the container.
4. **The adapter saved there is not the submission artifact.** The evaluation service only accepts complete model checkpoints (openpi JAX `params/` or PyTorch `model.safetensors`, plus `assets/physical-intelligence/libero/norm_stats.json`); a bare LoRA adapter is rejected by a CPU pre-check before evaluation. Merge the adapter into the π0.5 base and export the full checkpoint before running `rt.py upload`. See [docs/SUBNET_OVERVIEW.md](../docs/SUBNET_OVERVIEW.md).
5. **openpi modules are available** — the container ships with openpi installed; `import openpi.*` works out of the box.
6. **GPU is available** — torch and CUDA work normally inside the container.
7. **Temporary directories** — `/tmp` is usable inside the container but is lost on exit; persistent output must be written to `cfg['output_dir']`.

## Available imports

The container ships with these libraries pre-installed:

| Category | Libraries |
|---|---|
| Deep learning | `torch`, `torch.nn`, `torch.optim` |
| openpi | `openpi.shared.download`, `openpi.training.config`, `openpi.policies.policy_config`, `openpi.training.data_loader` |
| Data | `numpy`, `json`, `pickle` |
| System | `os`, `time`, `datetime`, `logging`, `importlib` |
