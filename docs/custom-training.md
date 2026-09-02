# Custom Training Strategy — Usage Guide

> **Status**: current · **Updated**: 2026-08-25 · **Audience**: miners writing their
> own training logic.
> **Scope**: the `train(cfg, episodes, policy)` contract and how your script reaches
> the training container. For the workflow around it, read
> [MINER.md](./MINER.md).

## Overview

Custom training scripts are injected into the container through a **volume mount**, so you can replace the training logic without rebuilding the Docker image.

```
host (miner)                          container (this season's image)
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
            - warmup_ratio: warmup ratio. ⚠️ Not passed down by
              `openroboto train`: only EPOCHS / BATCH_SIZE / LR / LORA_R /
              LORA_ALPHA reach the container, so this is always the runner's
              own default (0.05)
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

    # Export the checkpoint at the top of output_dir -- that directory is
    # uploaded verbatim as the HF repository root
    if hasattr(policy, "save_pretrained"):
        policy.save_pretrained(cfg["output_dir"])

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

    # Export the checkpoint at the top of output_dir
    policy.save_pretrained(cfg["output_dir"])

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

    # Export the checkpoint at the top of output_dir
    policy.save_pretrained(cfg["output_dir"])

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

### Option 1: `miner.yaml` (the normal way)

`openroboto init` already writes this, pointing at the `train_strategy.py` it
unpacked next to it:

```yaml
custom_train_script: "train_strategy.py"   # top level, not nested under `training:`
```

Then just run:

```bash
openroboto train
```

### Option 2: one run with a different script

```bash
openroboto train -s /path/to/my_strategy.py     # overrides custom_train_script
```

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
  <this-season-training-image>
```

The image name is not a constant: it comes from the season, as
`competition.params.training.image` in your `miner.yaml`. `openroboto build` builds
it from the build context this package ships for the season's base model.

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
3. **`cfg['output_dir']` is the checkpoint root.** `openroboto submit` uploads that directory verbatim as your Hugging Face repository root — nothing rearranges it afterwards. Export **at the top of it**, not into a subdirectory: the evaluator descends only two levels looking for the weights, and the LingBot exporter's own layout, `checkpoints/global_step_N/hf_ckpt/`, is already one level too deep. If your trainer insists on writing there, move the contents up before your `train()` returns. `openroboto train` prints the directory it found the weights in when they are not at the top.
   *(Earlier versions of this page said the save directory must be `cfg['output_dir']/adapter`. That was never true — nothing collected from that subdirectory — and it contradicted the next point.)*
4. **Export the full checkpoint, not a LoRA adapter.** The evaluator only accepts complete model checkpoints (openpi JAX `params/` or PyTorch `model.safetensors`, plus `assets/physical-intelligence/libero/norm_stats.json`; sharded safetensors plus `model.safetensors.index.json` for LingBot-VLA 2.0). A bare adapter is rejected before a GPU is allocated, and nothing merges it for you: there is **no `openroboto merge` command and there will not be one** — merging needs the model libraries, which cannot share an interpreter with bittensor, so it belongs in the training container next to the trainer that produced the weights. Run `openroboto check` before `openroboto submit`: it applies the evaluator's rules locally, for free. See [SUBNET_OVERVIEW.md](./SUBNET_OVERVIEW.md).
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
