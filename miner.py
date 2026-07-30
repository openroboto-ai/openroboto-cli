"""
miner.py - π0.5 LIBERO Miner (Steps 1-2 only) — REFERENCE SAMPLE

Workflow:
  1. Download control.json via HTTP (round state)
  2. Download dataset + π0.5 local training (LoRA by default)

⚠️  IMPORTANT — the default training output is a LoRA adapter, which is NOT
directly submittable. The evaluation worker only accepts complete model
checkpoints and rejects bare adapters at a pre-eval check (no GPU time spent,
rejection reason returned to the miner).

Before running rt.py, merge the adapter into the π0.5 base and export a FULL
checkpoint into the round output directory, so that it contains:
  - openpi JAX:      params/           (orbax OCDBT), or
  - openpi PyTorch:  model.safetensors
  - plus:            assets/physical-intelligence/libero/norm_stats.json

Verify locally before paying the submission fee (pure CPU, from the public
evaluation repo github.com/openroboto-ai/openroboto-evaluation):
    uv run libero_eval/check_model.py --model <output_dir> --config pi05_libero

See docs/SUBNET_OVERVIEW.md §3 "What the uploaded repo must contain".

After training completes, state is saved to state/round_N.json.
Run rt.py for steps 3-5: upload to HF, stake burn, chain announcement.

Usage:
    python miner.py --config miner.yaml
"""

import os
import sys
import argparse
import tempfile
import logging
import json
import time
import shutil
from datetime import datetime, timezone
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from utils.config import Config
from utils.logger import setup_logger
from utils.http_downloader import download_file
from miner.trainer_vla import train_vla

logger = setup_logger("miner")


def parse_args():
    parser = argparse.ArgumentParser(description="π0.5 LIBERO Miner (Steps 1-2)")
    parser.add_argument("--config", type=str, default="miner.yaml")
    parser.add_argument("--once", action="store_true", help="Run one round and exit")
    parser.add_argument("--output-dir", type=str, default="./tmp/robot_train_vla_miner")
    return parser.parse_args()


# ─── Round State File ──────────────────────────────────────

STATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state")


def _state_path(round_num: int) -> str:
    return os.path.join(STATE_DIR, f"round_{round_num}.json")


def _load_state(round_num: int) -> dict:
    path = _state_path(round_num)
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_state(round_num: int, state: dict) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(_state_path(round_num), "w") as f:
        json.dump(state, f, indent=2)


def _clear_state(round_num: int) -> None:
    path = _state_path(round_num)
    if os.path.exists(path):
        os.remove(path)


def _is_step_done(state: dict, step: str) -> bool:
    return state.get("step") == step and state.get("status") == "completed"


def fetch_control(url: str, last_etag: str = "") -> tuple:
    """通过 HTTP 下载 control.json / Download control.json via HTTP; return (None, etag) if ETag unchanged."""
    logger.info(f"[control] Fetching control.json from {url}")
    try:
        import urllib.request
        req = urllib.request.Request(url)
        req.add_header("User-Agent", "robot-train-subnet/0.5")
        if last_etag:
            req.add_header("If-None-Match", last_etag)
        resp = urllib.request.urlopen(req, timeout=30)
        etag = resp.headers.get("ETag", "").strip('"')
        logger.debug(f"[fetch_control] HTTP {resp.status} | ETag: {etag or '(none)'}")
        if resp.status == 304:
            logger.info(f"[control] control.json unchanged (304 Not Modified, ETag={last_etag})")
            return None, last_etag
        data = json.loads(resp.read().decode("utf-8"))
        rnd = data.get("round", 0)
        status = data.get("status", "")
        pub_key = data.get("public_key", "")
        burn_rate = data.get("payment", {}).get("burn_rate_tao", 0)
        logger.info(
            f"[control] control.json fetched OK | round={rnd} status={status} "
            f"burn_rate={burn_rate} TAO public_key={'set' if pub_key else 'empty'} ETag={etag or '(none)'}"
        )
        return data, etag if etag else last_etag
    except Exception as e:
        logger.warning(f"[control] Fetch failed: {e}")
        return None, last_etag


def run_one_round(
    config: Config, control: dict,
    output_dir: str,
) -> dict:
    """执行一轮 π0.5 LIBERO 训练（支持断点续训） / Execute one round of π0.5 LIBERO training with resume support.

    Steps 1-2 only: Preparation + Training.
    Saves state for rt.py to handle Steps 3-5.
    """
    round_num = control["round"]
    dataset = control.get("dataset", {})
    training = control.get("training", {})

    # Load state for resume
    state = _load_state(round_num)
    last_step = state.get("step", "")
    logger.info(f"[round {round_num}] Resume state: step={last_step or '(none)'}")

    # ─── Prepare step ─────────────────────────────────────
    if not _is_step_done(state, "prep"):
        logger.info("[round %d] 📦 Step 1/2: Preparation", round_num)
        state["round"] = round_num
        state["step"] = "prep"
        state["status"] = "in_progress"
        state["started_at"] = datetime.now(timezone.utc).isoformat()

        vla_checkpoint_path = training.get("vla_checkpoint_path", "") or getattr(config, "vla_checkpoint_path", "")
        if not vla_checkpoint_path:
            from protocol.types import PI05_BASE_CHECKPOINT
            vla_checkpoint_path = PI05_BASE_CHECKPOINT

        # Checkpoint cache
        local_ckpt = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache", "pi05_base")
        if vla_checkpoint_path.startswith("gs://"):
            if os.path.exists(local_ckpt) and os.listdir(local_ckpt):
                logger.info(f"✅ Using cached checkpoint: {local_ckpt}")
            else:
                logger.info(f"📥 No local cache, mounting cache dir for in-container download: {local_ckpt}")
                os.makedirs(local_ckpt, exist_ok=True)
            vla_checkpoint_path = local_ckpt
        state["checkpoint_path"] = vla_checkpoint_path

        # Training params
        state["epochs"] = training.get("epochs", 3)
        state["batch_size"] = training.get("batch_size", 4)
        state["lr"] = training.get("learning_rate", 1e-4)
        state["lora_r"] = training.get("lora_r", 32)
        state["lora_alpha"] = training.get("lora_alpha", 64)
        state["data_version"] = dataset.get("version", f"v{round_num}")

        # Dataset download
        train_url = dataset.get("train_url") or config.dataset_train_url
        val_url = dataset.get("val_url") or config.dataset_val_url
        if not train_url:
            logger.error("❌ Training dataset URL not configured")
            state["status"] = "failed"
            state["error"] = "train_url missing"
            _save_state(round_num, state)
            return {}

        round_output = os.path.join(output_dir, f"round_{round_num}")
        os.makedirs(round_output, exist_ok=True)
        state["round_output"] = round_output
        state["train_url"] = train_url
        state["val_url"] = val_url or ""

        state["step"] = "prep"
        state["status"] = "completed"
        _save_state(round_num, state)
        logger.info("[round %d] ✅ Preparation complete", round_num)
    else:
        logger.info("[round %d] ⏭️  Step 1/2: Preparation (skipped)", round_num)
        state = _load_state(round_num)
        vla_checkpoint_path = state.get("checkpoint_path", "")
        round_output = state.get("round_output", os.path.join(output_dir, f"round_{round_num}"))

    # ─── Training step ────────────────────────────────────
    if not _is_step_done(state, "training"):
        logger.info("[round %d] 🚀 Step 2/2: Model Training", round_num)
        state["step"] = "training"
        state["status"] = "in_progress"
        _save_state(round_num, state)

        hf_token = getattr(config, "hf_token", "") or ""
        if not hf_token:
            logger.error("❌ HF token not configured, cannot upload model")
            state["status"] = "failed"
            state["error"] = "hf_token missing"
            _save_state(round_num, state)
            return {}

        train_cfg = SimpleNamespace(
            hotkey=config.hotkey,
            model_cache_dir=getattr(config, "model_cache_dir", "~/.cache/openpi"),
            lora_r=state.get("lora_r", 32), lora_alpha=state.get("lora_alpha", 64),
            lora_dropout=training.get("lora_dropout", 0.1),
            batch_size=state.get("batch_size", 4), learning_rate=state.get("lr", 1e-4),
            epochs=state.get("epochs", 3),
            warmup_ratio=training.get("warmup_ratio", 0.05),
            vla_checkpoint_path=vla_checkpoint_path,
        )

        # Download datasets in a temp dir
        with tempfile.TemporaryDirectory() as tmpdir:
            train_path = download_file(state["train_url"], os.path.join(tmpdir, "train.json"))
            logger.info(f"[round %d] 📥 Training dataset downloaded", round_num)

            val_path = None
            if state.get("val_url"):
                try:
                    val_path = download_file(state["val_url"], os.path.join(tmpdir, "val.json"))
                    logger.info(f"[round %d] 📥 Validation dataset downloaded", round_num)
                except Exception:
                    logger.info("[round %d] ⏭️  No validation dataset", round_num)

            logger.info("[round %d] 🔥 Starting training...", round_num)

            # Detect custom training strategy from config
            custom_strategy = config.custom_train_script
            if custom_strategy and os.path.exists(custom_strategy):
                logger.info(f"[round %d] 📎 Using custom training strategy: {custom_strategy}")
            elif custom_strategy:
                logger.warning(f"[round %d] ⚠️  custom_train_script set but file not found: {custom_strategy}")
                custom_strategy = None

            metrics, pot = train_vla(
                checkpoint_path=vla_checkpoint_path,
                train_json_path=train_path,
                output_dir=round_output,
                config=train_cfg,
                eval_json_path=val_path,
                hf_token=hf_token,
                custom_train_script=custom_strategy,
            )
            logger.info(f"[round %d] 📊 Training complete | final_loss={metrics.get('final_loss', 0):.4f} | steps={pot.get('total_steps', 0)}", round_num)

        # Validate training result
        if not metrics or not metrics.get("final_loss") or metrics.get("final_loss", 0) == 0:
            logger.error(f"[round %d] ❌ Training result invalid - metrics={metrics}. Possible GPU OOM or other training failure.", round_num)
            state["status"] = "failed"
            state["error"] = "training_result_invalid"
            _save_state(round_num, state)
            return {}

        state["training_metrics"] = metrics
        state["training_proof"] = pot
        state["step"] = "training"
        state["status"] = "completed"
        _save_state(round_num, state)
        logger.info(f"[round %d] ✅ Training complete — model saved at {round_output}", round_num)
        logger.warning(
            "[round %d] ⚠️  The default training output is a LoRA adapter and is NOT directly submittable. "
            "Merge it into the π0.5 base and export a FULL checkpoint (params/ or model.safetensors "
            "+ assets/physical-intelligence/libero/norm_stats.json) into the round output dir, "
            "then verify with check_model.py. See docs/SUBNET_OVERVIEW.md §3.",
            round_num,
        )
        logger.info(f"[round %d] 📝 Run 'python rt.py submit --round {round_num}' for steps 3-5 (upload → burn → announce)", round_num)
    else:
        logger.info("[round %d] ⏭️  Step 2/2: Model Training (skipped)", round_num)
        state = _load_state(round_num)

    metrics = state.get("training_metrics", {})
    return metrics


def main():
    args = parse_args()
    logger.debug(f"[main] args: config={args.config}, once={args.once}, output_dir={args.output_dir}")

    # Clean temp files from previous run to prevent accumulation
    tmp_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tmp")
    if os.path.exists(tmp_dir):
        logger.info(f"🧹 Cleaning tmp directory: {tmp_dir}")
        shutil.rmtree(tmp_dir, ignore_errors=True)
    logger.info(f"[main] Tmp directory ready: {tmp_dir}")

    config = Config.load(args.config)
    logger.debug(f"[main] config loaded | network={config.network} netuid={config.netuid} hotkey={config.hotkey}")

    # HTTP direct link: control.json URL (required)
    control_url = config.control_json_url
    logger.debug(f"[main] control_json_url={control_url}")
    if not control_url:
        logger.error("❌ control_json_url not configured (miner.conf: urls.control_json)")
        sys.exit(1)

    logger.info(f"[main] Reading control.json for the first time...")
    control, etag = fetch_control(control_url)
    if not control:
        logger.error("❌ Cannot read control.json, exiting")
        sys.exit(1)

    # Apply miner-visible public control.json fields (payment, training, etc.)
    config.apply_control(control)
    logger.info(f"[main] control.json applied | round={control.get('round')} status={control.get('status')} burn_rate={config.burn_rate_tao} TAO")

    rnd = control.get("round", 0)
    status = control.get("status", "")
    poll_sec = control.get("process", {}).get("watch_poll_seconds", 30)

    config.apply_control(control)
    logger.info(f"💰 Payment config: burn_rate={config.burn_rate_tao} TAO, limit_price={config.limit_price_rao}")

    logger.info(f"🦞 π0.5 Miner started | hotkey={config.hotkey} | HF={config.hf_username}")
    logger.info(f"👁️  Detected round={rnd} status={status} poll_sec={poll_sec}")

    if status == "active":
        logger.info(f"[main] Starting training Round {rnd}")
        try:
            result = run_one_round(
                config=config, control=control,
                output_dir=args.output_dir,
            )
            if result:
                logger.info(f"[main] Round {rnd} training complete — run rt.py for steps 3-5")
            else:
                logger.error(f"❌ Round {rnd} training failed (empty result)")
                sys.exit(1)
        except Exception as e:
            logger.error(f"❌ Round {rnd} failed: {e}", exc_info=True)
            sys.exit(1)
    else:
        logger.warning(f"⚠️  Round {rnd} status is '{status}', not 'active'. Exiting.")
        sys.exit(1)


if __name__ == "__main__":
    main()
