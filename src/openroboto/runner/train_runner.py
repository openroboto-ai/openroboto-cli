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
    """Read training config from environment variables.

    Returns:
        Config dict containing checkpoint_path, train_data, epochs, batch_size, etc.
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
    """Execute π₀.₅ LIBERO training, returns (metrics, proof).

    Uses a custom training script if the CUSTOM_TRAIN env var is set, otherwise runs
    the default flow.

    Args:
        cfg: configuration dictionary from get_config()

    Returns:
        (metrics, proof) two dicts
    """

    custom_train = os.getenv("CUSTOM_TRAIN", "")
    if custom_train and os.path.exists(custom_train):
        logger.info(f"🔧 Using custom training script: {custom_train}")
        return _run_custom(cfg, custom_train)
    return _run_default(cfg)


def _run_custom(cfg: dict, custom_script: str) -> tuple:
    """Load and execute an externally mounted custom training script.

    Script interface contract:
        def train(cfg: dict, episodes: list, policy) -> tuple:
            # cfg: config dict read from env vars
            # episodes: loaded training data list
            # policy: openpi policy (checkpoint already downloaded)
            # return: (metrics_dict, proof_dict)

    Args:
        cfg: configuration dictionary
        custom_script: path to the custom training script

    Returns:
        (metrics, proof) two dicts
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
    """Default flow when no custom strategy is mounted: a simulated loss curve,
    and no checkpoint. For exercising the pipeline, not for training.

    Args:
        cfg: configuration dictionary

    Returns:
        (metrics, proof) dicts holding the training metrics and the proof
    """

    # Download checkpoint if needed
    cp = cfg["checkpoint_path"]
    logger.info(f"📦 π₀.₅ checkpoint: {cp}")

    # Load training data
    episodes = _load_episodes(cfg["train_data"])
    logger.info(f"📊 Loaded {len(episodes)} episodes")

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

    # No checkpoint is exported here on purpose.
    #
    # This path is the smoke test: it runs without a GPU and without training
    # anything, so there are no weights to write. It used to fabricate a LoRA
    # adapter under `output_dir/adapter/`, which was wrong twice over -- an
    # adapter is never a submittable artifact (nothing merges it), and the
    # subdirectory taught the wrong export location. `output_dir` **is** the
    # checkpoint root; a real run's export writes the full checkpoint at the top
    # of it.
    #
    # Fabricating a `model.safetensors` full of random numbers instead would be
    # worse than either: `openroboto check` would go green, and that command is
    # the last thing standing between a miner and a burn of TAO.
    #
    # The norm_stats that used to be written here went with it: `_save_norm_stats`
    # was **never defined in this file**, so every run of this function died on a
    # NameError at that line -- which is why nobody noticed the fabricated
    # adapter was useless. (ruff would have caught it as F821; `src/openroboto/
    # runner` is in `extend-exclude`.) Norm stats without weights buy nothing
    # anyway: `check` reports the missing weights either way.
    output_dir = cfg["output_dir"]
    os.makedirs(output_dir, exist_ok=True)
    logger.warning(
        "⚠️  No checkpoint exported — this is the default smoke-test flow, it "
        "does not train. `openroboto check` will report the missing weights."
    )

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
        # Measured here because here is where the training happened. The CLI on
        # the host cannot see it: `docker run --gpus all` puts the process in
        # another namespace, so a host-side `torch.cuda.max_memory_allocated()`
        # reads its own (empty) context and reports 0.0 -- which then went into
        # the miner's public training proof as if the run had used no VRAM.
        "gpu_memory_peak_gb": _peak_vram_gb(),
    }

    with open(os.path.join(output_dir, "proof.json"), "w") as f:
        json.dump(proof, f, indent=2)

    logger.info(f"✅ Training complete in {duration:.1f}s | loss={final_loss:.4f}")
    return metrics, proof


def _load_episodes(path: str) -> list:
    """Load episode JSON data from the given path.

    Args:
        path: path to the JSON file

    Returns:
        list of episodes
    """
    with open(path, "r") as f:
        data = json.load(f)
    if isinstance(data, dict):
        data = data.get("episodes", data.get("data", [data]))
    return data


def _compute_norm_stats(episodes: list) -> dict:
    """Compute norm stats from training data, matching openpi expected format.

    Returns a dict[str, NormStats] compatible with openpi's Normalize transform.

    Args:
        episodes: Episode list.
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
        """Compute mean, std, q01 (1st percentile), q99 (99th percentile) and
        return a NormStats object."""
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
    """Get GPU device name via torch, or return 'cpu' if unavailable.

    Returns:
        GPU device name string
    """
    try:
        import torch
        if torch.cuda.is_available():
            return torch.cuda.get_device_name(0)
    except ImportError:
        pass
    return "cpu"


def _peak_vram_gb() -> float:
    """Peak VRAM this process allocated, in GB. 0.0 when there is no CUDA.

    Pairs with `_get_gpu_name()`: both describe the machine that ran the
    training, and both have to be read inside the container for the same reason.
    `max_memory_allocated` is cumulative for the process, so calling it after the
    loop gives the run's peak without instrumenting the loop itself.
    """
    try:
        import torch
        if torch.cuda.is_available():
            return round(torch.cuda.max_memory_allocated() / 1024**3, 2)
    except ImportError:
        pass
    return 0.0


# ─── Main ─────────────────────────────────────────────────

def main():
    """Main entry point: init logging, read config, run training, print JSON result."""
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
