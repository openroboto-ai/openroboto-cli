#!/usr/bin/env python3
"""
rt.py — RobotTrain Post-Training CLI

Steps 3-5: Upload model to HuggingFace, Stake Burn, Chain Announcement.
Run after miner.py completes Step 1-2 (preparation + training).

Usage:
    # Full pipeline: upload → burn → announce (steps 3-5)
    python rt.py submit --config miner.yaml

    # Step 3 only: upload model to HuggingFace
    python rt.py upload --config miner.yaml --round 1

    # Step 4 only: stake burn
    python rt.py burn --config miner.yaml

    # Step 5 only: chain commitment
    python rt.py announce --config miner.yaml --round 1
"""

import argparse
import json
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from utils.config import Config
from utils.logger import setup_logger
from utils.chain import get_subtensor, get_wallet, submit_hf_model_announcement
from payment import execute_stake_burn

logger = setup_logger("rt")

# ─── Wallet password helper ────────────────────────────────

def _read_wallet_password(config, prompt: str = "Enter wallet password: ", max_retries: int = 3, timeout_sec: float = 60.0) -> str:
    """
    Read wallet password from config or interactive prompt.

    - If config has wallet_password, use it directly.
    - Otherwise, prompt interactively with timeout.
    - On wrong password, retry up to max_retries times.
    - On timeout or max retries reached, log error and exit.
    """
    if config.wallet_password:
        return config.wallet_password

    import getpass
    import threading

    for attempt in range(1, max_retries + 1):
        result = {"password": None, "error": None}

        def _prompt():
            try:
                pw = getpass.getpass(f"{prompt} (attempt {attempt}/{max_retries}): ")
                result["password"] = pw
            except Exception as e:
                result["error"] = str(e)

        t = threading.Thread(target=_prompt, daemon=True)
        t.start()
        t.join(timeout=timeout_sec)

        if t.is_alive():
            logger.error(f"⏱️  Password input timeout ({timeout_sec}s). Too slow.")
            logger.error(f"   Tip: set `wallet_password` in miner.yaml to skip interactive prompt.")
            if attempt < max_retries:
                logger.info(f"   Retrying... ({attempt}/{max_retries})")
            continue

        if result.get("error"):
            logger.error(f"❌ Password input error: {result['error']}")
            if attempt < max_retries:
                logger.info(f"   Retrying... ({attempt}/{max_retries})")
            continue

        if result["password"]:
            # Verify password by attempting to load wallet
            try:
                test_wallet = get_wallet(config.coldkey, config.hotkey, config.wallet_path,
                                         password=result["password"])
                # If we get here, password is correct
                logger.info(f"✅ Wallet password verified")
                return result["password"]
            except Exception as e:
                err_msg = str(e).lower()
                if "password" in err_msg or "decrypt" in err_msg or "key" in err_msg or "incorrect" in err_msg:
                    logger.warning(f"❌ Wrong password (attempt {attempt}/{max_retries})")
                else:
                    logger.error(f"❌ Wallet load error: {e}")
                if attempt < max_retries:
                    logger.info(f"   Retrying... ({attempt}/{max_retries})")
                continue

        # Empty password
        logger.warning(f"⚠️  Empty password entered (attempt {attempt}/{max_retries})")
        if attempt < max_retries:
            logger.info(f"   Retrying... ({attempt}/{max_retries})")

    logger.error(f"❌ Wallet password: {max_retries} failed attempts — aborting")
    logger.error(f"   Tip: set `wallet_password` in miner.yaml to skip interactive prompt.")
    sys.exit(1)

STATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state")


def _load_state(round_num: int) -> dict:
    """Load miner state file."""
    path = os.path.join(STATE_DIR, f"round_{round_num}.json")
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_state(round_num: int, state: dict) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(os.path.join(STATE_DIR, f"round_{round_num}.json"), "w") as f:
        json.dump(state, f, indent=2)


def _resolve_round(args) -> int:
    if args.round and args.round > 0:
        return args.round
    # Auto-detect from state
    for fname in sorted(os.listdir(STATE_DIR), reverse=True) if os.path.exists(STATE_DIR) else []:
        if fname.startswith("round_") and fname.endswith(".json"):
            try:
                rn = int(fname.split("_")[1].split(".")[0])
                state = _load_state(rn)
                if state.get("step") == "training" and state.get("status") == "completed":
                    return rn
            except Exception:
                pass
    logger.error("❌ Cannot auto-detect round. Use --round N")
    sys.exit(1)


def _resolve_output_dir(round_num: int) -> str:
    state = _load_state(round_num)
    return state.get("round_output", f"./tmp/robot_train_vla_miner/round_{round_num}")


def _metrics(round_num: int) -> dict:
    state = _load_state(round_num)
    return state.get("training_metrics", {})


# ─── Step 3: Upload ────────────────────────────────────────

def cmd_upload(args):
    """Upload model to HuggingFace (Step 3)."""
    config = Config.load(args.config)
    round_num = _resolve_round(args)
    output_dir = args.output_dir or _resolve_output_dir(round_num)

    from miner.push_hf import push_model_to_hf
    from miner import build_hf_repo_id

    logger.info(f"[rt] Step 3/3: Upload model to HuggingFace")
    logger.info(f"  round={round_num} dir={output_dir}")

    hf_token = config.hf_token
    if not hf_token:
        logger.error("❌ HF token not configured")
        sys.exit(1)

    hf_repo_id = build_hf_repo_id(config, round_num)
    metrics = _metrics(round_num)

    hf_url = push_model_to_hf(
        model_dir=output_dir, repo_id=hf_repo_id,
        hf_token=hf_token, round_num=round_num, metrics=metrics,
    )
    if not hf_url:
        logger.error("❌ HF upload failed")
        sys.exit(1)

    # Get commit hash
    from huggingface_hub import HfApi
    api = HfApi(token=hf_token)
    repo_info = api.repo_info(hf_repo_id, repo_type="model")
    hf_commit = repo_info.sha

    # Save state
    state = _load_state(round_num)
    state["hf_repo_id"] = hf_repo_id
    state["hf_url"] = hf_url
    state["hf_commit"] = hf_commit
    state["step"] = "upload"
    state["status"] = "completed"
    _save_state(round_num, state)

    logger.info(f"✅ Uploaded: {hf_url}")
    logger.info(f"   commit: {hf_commit[:8]}")


# ─── Step 4: Burn ──────────────────────────────────────────

def cmd_burn(args):
    """Stake burn on-chain (Step 4)."""
    config = Config.load(args.config)

    logger.info(f"[rt] Step 4/3: Stake Burn Payment")

    password = _read_wallet_password(config)
    subtensor = get_subtensor(config.network)
    wallet = get_wallet(config.coldkey, config.hotkey, config.wallet_path,
                        password=password)

    burn_rate_tao = getattr(config, "burn_rate_tao", 0.01)
    limit_price_rao = getattr(config, "limit_price_rao", 0)

    burn_result = execute_stake_burn(
        subtensor=subtensor, wallet=wallet, netuid=config.netuid,
        amount_tao=burn_rate_tao, limit_price_rao=limit_price_rao,
    )
    if subtensor:
        subtensor.close()

    if not burn_result or not burn_result.get("tx_hash"):
        logger.error("❌ Stake burn failed")
        sys.exit(1)

    burn_tx = burn_result["tx_hash"]
    burn_block = burn_result.get("block_number")
    logger.info(f"✅ Burn submitted: tx={burn_tx[:16]}... block={burn_block}")


# ─── Step 5: Announce ──────────────────────────────────────

def cmd_announce(args):
    """Chain commitment with block_hash (Step 5).
    State only: reads hf_repo_id/hf_url/burn_tx from state file.
    Manual override removed to prevent announcing without training."""
    config = Config.load(args.config)
    round_num = _resolve_round(args)

    state = _load_state(round_num)
    metrics = _metrics(round_num)

    # State only — no CLI override to prevent announcing without training
    hf_repo_id = state.get("hf_repo_id", "")
    hf_url = state.get("hf_url", "")
    burn_tx = state.get("burn_tx_hash", "")

    if not hf_repo_id or not hf_url:
        logger.error("❌ HF repo/URL not found. Run 'rt.py upload' first.")
        sys.exit(1)

    logger.info(f"[rt] Step 5/3: Chain Commitment")
    logger.info(f"  round={round_num} repo={hf_repo_id}")
    if burn_tx:
        logger.info(f"  burn_tx={burn_tx[:16]}...")

    password = _read_wallet_password(config)
    subtensor = get_subtensor(config.network)
    wallet = get_wallet(config.coldkey, config.hotkey, config.wallet_path,
                        password=password)

    # Get block hash for reveal
    current_block = subtensor.get_current_block()
    block_hash = subtensor.get_block_hash(current_block)
    logger.info(f"  block={current_block} block_hash={block_hash[:16]}...")

    result = submit_hf_model_announcement(
        subtensor=subtensor, wallet=wallet, netuid=config.netuid,
        hf_repo_id=hf_repo_id, hf_url=hf_url, round_num=round_num,
        metrics=metrics, burn_tx_hash=burn_tx, burn_block=burn_block,
        block_hash=block_hash, hotkey_ss58=config.hotkey_ss58,
    )
    if subtensor:
        subtensor.close()

    if not result.get("ok"):
        logger.error("❌ Chain commitment failed")
        sys.exit(1)

    ext_hash = result.get("extrinsic_hash", "")
    block_h = result.get("block_height", 0)
    logger.info(f"✅ Commitment submitted | block={block_h} ext=0x{ext_hash[:16]}...")

    # Update state
    state["burn_tx_hash"] = burn_tx
    state["step"] = "announce"
    state["status"] = "completed"
    _save_state(round_num, state)


def cmd_submit(args):
    """Full pipeline: upload → burn → announce (Steps 3-5)."""
    config = Config.load(args.config)
    round_num = _resolve_round(args)
    output_dir = args.output_dir or _resolve_output_dir(round_num)
    metrics = _metrics(round_num)

    logger.info(f"🦞 rt.py submit | round={round_num}")

    # Step 3: Upload
    state = _load_state(round_num)
    if state.get("step") == "announce" and not args.force:
        logger.info("⏭️  Already complete, skipping")
        return

    if args.force:
        logger.info("⚡ --force: re-running full pipeline (upload → burn → announce)")

    from miner.push_hf import push_model_to_hf
    from miner import build_hf_repo_id

    hf_token = config.hf_token
    if not hf_token:
        logger.error("❌ HF token not configured")
        sys.exit(1)

    hf_repo_id = state.get("hf_repo_id", "")
    hf_url = state.get("hf_url", "")
    hf_commit = state.get("hf_commit", "")

    if not hf_repo_id or not hf_url:
        logger.info(f"[rt] Step 3/3: Upload to HuggingFace")
        hf_repo_id = build_hf_repo_id(config, round_num)
        hf_url = push_model_to_hf(
            model_dir=output_dir, repo_id=hf_repo_id,
            hf_token=hf_token, round_num=round_num, metrics=metrics,
        )
        if not hf_url:
            logger.error("❌ HF upload failed")
            sys.exit(1)
        from huggingface_hub import HfApi
        api = HfApi(token=hf_token)
        repo_info = api.repo_info(hf_repo_id, repo_type="model")
        hf_commit = repo_info.sha
        state["hf_repo_id"] = hf_repo_id
        state["hf_url"] = hf_url
        state["hf_commit"] = hf_commit
        state["step"] = "upload"
        state["status"] = "completed"
        _save_state(round_num, state)
        logger.info(f"✅ Uploaded: {hf_url}")
    else:
        logger.info(f"⏭️  Upload complete: {hf_url}")

    # Step 4: Burn (--force re-runs burn)
    burn_tx = "" if args.force else state.get("burn_tx_hash", "")
    burn_block = state.get("burn_block", 0)
    if not burn_tx:
        logger.info(f"[rt] Step 4/3: Stake Burn Payment")
        password = _read_wallet_password(config)
        subtensor = get_subtensor(config.network)
        wallet = get_wallet(config.coldkey, config.hotkey, config.wallet_path,
                            password=password)
        burn_rate_tao = getattr(config, "burn_rate_tao", 0.01)
        limit_price_rao = getattr(config, "limit_price_rao", 0)
        burn_result = execute_stake_burn(
            subtensor=subtensor, wallet=wallet, netuid=config.netuid,
            amount_tao=burn_rate_tao, limit_price_rao=limit_price_rao,
        )
        if subtensor:
            subtensor.close()
        if not burn_result or not burn_result.get("tx_hash"):
            logger.error("❌ Stake burn failed")
            sys.exit(1)
        burn_tx = burn_result["tx_hash"]
        burn_block = burn_result.get("block_number")
        state["burn_tx_hash"] = burn_tx
        state["burn_block"] = burn_block
        _save_state(round_num, state)
        logger.info(f"✅ Burn submitted: tx={burn_tx[:16]}... block={burn_block}")
    else:
        logger.info(f"⏭️  Burn complete: tx={burn_tx[:16]}... block={burn_block}")

    # Step 5: Announce
    logger.info(f"[rt] Step 5/3: Chain Commitment")
    password = _read_wallet_password(config)
    subtensor = get_subtensor(config.network)
    wallet = get_wallet(config.coldkey, config.hotkey, config.wallet_path,
                        password=password)

    current_block = subtensor.get_current_block()
    block_hash = subtensor.get_block_hash(current_block)
    logger.info(f"  block={current_block} block_hash={block_hash[:16]}...")

    result = submit_hf_model_announcement(
        subtensor=subtensor, wallet=wallet, netuid=config.netuid,
        hf_repo_id=hf_repo_id, hf_url=hf_url, round_num=round_num,
        metrics=metrics, burn_tx_hash=burn_tx, burn_block=burn_block,
        block_hash=block_hash, hotkey_ss58=config.hotkey_ss58,
    )
    if subtensor:
        subtensor.close()

    if not result.get("ok"):
        logger.error("❌ Chain commitment failed")
        sys.exit(1)

    ext_hash = result.get("extrinsic_hash", "")
    block_h = result.get("block_height", 0)
    logger.info(f"✅ Commitment submitted | block={block_h} ext=0x{ext_hash[:16]}...")

    state["step"] = "announce"
    state["status"] = "completed"
    _save_state(round_num, state)


def build_parser():
    parser = argparse.ArgumentParser(prog="rt", description="RobotTrain Post-Training CLI")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("submit", help="Full pipeline: upload → burn → announce")
    p.add_argument("--config", default="miner.yaml")
    p.add_argument("--round", type=int, default=0, help="Round (auto-detect from state)")
    p.add_argument("--output-dir", default="", help="Model output dir (auto from state)")
    p.add_argument("--force", action="store_true", help="Re-run even if already complete (re-announce, skip burn)")
    p.set_defaults(func=cmd_submit)

    p = sub.add_parser("upload", help="Step 3: Upload model to HF")
    p.add_argument("--config", default="miner.yaml")
    p.add_argument("--round", type=int, default=0, help="Round")
    p.add_argument("--output-dir", default="", help="Model output dir")
    p.set_defaults(func=cmd_upload)

    p = sub.add_parser("burn", help="Step 4: Stake Burn")
    p.add_argument("--config", default="miner.yaml")
    p.set_defaults(func=cmd_burn)

    p = sub.add_parser("announce", help="Step 5: Chain Commitment with block_hash")
    p.add_argument("--config", default="miner.yaml")
    p.add_argument("--round", type=int, default=0, help="Round")
    p.set_defaults(func=cmd_announce)

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)
    args.func(args)


if __name__ == "__main__":
    main()
