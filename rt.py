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
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from utils.config import Config
from utils.logger import setup_logger
from utils.chain import get_subtensor, get_wallet, submit_hf_model_announcement
from payment import execute_stake_burn

logger = setup_logger("rt")

# ─── Wallet password helper ────────────────────────────────

def _read_wallet_password(config, prompt: str = "Enter wallet password: ", max_retries: int = 3, timeout_sec: float = 60.0) -> str:
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
            try:
                test_wallet = get_wallet(config.coldkey, config.hotkey, config.wallet_path,
                                         password=password)
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

        logger.warning(f"⚠️  Empty password entered (attempt {attempt}/{max_retries})")
        if attempt < max_retries:
            logger.info(f"   Retrying... ({attempt}/{max_retries})")

    logger.error(f"❌ Wallet password: {max_retries} failed attempts — aborting")
    logger.error(f"   Tip: set `wallet_password` in miner.yaml to skip interactive prompt.")
    sys.exit(1)

STATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state")


# ─── Control.json fetcher for rt.py ──────────────────────────

def fetch_control_and_apply(config, last_etag=""):
    """Fetch control.json via HTTP and apply_control to update burn_rate_tao."""
    logger.info(f"[rt] Fetching control.json from {config.control_json_url}")
    try:
        req = urllib.request.Request(config.control_json_url)
        req.add_header("User-Agent", "robot-train-subnet/0.5")
        if last_etag:
            req.add_header("If-None-Match", last_etag)
        resp = urllib.request.urlopen(req, timeout=30)
        etag = resp.headers.get("ETag", "").strip('"')
        if resp.status == 304:
            logger.info(f"[rt] control.json unchanged (304)")
            return None, last_etag
        data = json.loads(resp.read().decode("utf-8"))
        config.apply_control(data)
        burn_rate = getattr(config, "burn_rate_tao", 0.01)
        logger.info(f"[rt] control.json applied | burn_rate_tao={burn_rate}")
        return data, etag if etag else last_etag
    except Exception as e:
        logger.warning(f"[rt] Fetch control.json failed: {e}")
        return None, last_etag


# ─── Announce pre-check ────────────────────────────────────

def check_announce_ready(state, round_num):
    """Pre-check all announce prerequisites before spending on burn."""
    reasons = []

    hf_repo_id = state.get("hf_repo_id", "")
    hf_url = state.get("hf_url", "")
    hf_commit = state.get("hf_commit", "")

    if not hf_repo_id:
        reasons.append("hf_repo_id missing — run 'rt.py upload' first")
    if not hf_url:
        reasons.append("hf_url missing — run 'rt.py upload' first")
    if not hf_commit or len(hf_commit) != 40:
        reasons.append(f"hf_commit invalid ({hf_commit[:12] if hf_commit else 'empty'}, expected 40 hex chars)")

    if hf_repo_id:
        hotkey_ss58 = state.get("hotkey_ss58", "")
        if not hotkey_ss58:
            reasons.append("hotkey_ss58 missing in state — re-run 'rt.py upload' to populate")
        else:
            block_hash = state.get("block_hash", "")
            burn_tx_hash = state.get("burn_tx_hash", "")
            burn_block = state.get("burn_block", 0) or 0

            payload_data = {
                "s": hotkey_ss58,
                "h": block_hash[2:] if block_hash.startswith("0x") else block_hash,
                "c": hf_commit if hf_commit else "",
                "r": round_num,
                "i": hf_repo_id,
                "b": burn_tx_hash[2:] if burn_tx_hash and burn_tx_hash.startswith("0x") else (burn_tx_hash or ""),
                "bb": burn_block if burn_block else None,
            }

            try:
                payload_bytes = json.dumps(payload_data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
                payload_size = len(payload_bytes)
                if payload_size > 512:
                    reasons.append(
                        f"commitment payload too large ({payload_size} bytes > 512 limit). "
                        f"Check hf_repo_id length."
                    )
                else:
                    logger.info(f"[rt] Pre-check OK: commitment payload = {payload_size}/512 bytes")
            except Exception as e:
                reasons.append(f"commitment payload encoding failed: {e}")

    if reasons:
        logger.error(f"❌ Announce pre-check FAILED (round {round_num}):")
        for r in reasons:
            logger.error(f"   • {r}")
        logger.error(f"   → Will NOT proceed with burn. Fix the issues above first.")
        sys.exit(1)

    logger.info(f"✅ Announce pre-check passed for round {round_num}")


def _load_state(round_num):
    path = os.path.join(STATE_DIR, f"round_{round_num}.json")
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_state(round_num, state):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(os.path.join(STATE_DIR, f"round_{round_num}.json"), "w") as f:
        json.dump(state, f, indent=2)


def _resolve_round(args):
    if args.round and args.round > 0:
        return args.round
    rounds = []
    if os.path.exists(STATE_DIR):
        for fname in os.listdir(STATE_DIR):
            if fname.startswith("round_") and fname.endswith(".json"):
                try:
                    rounds.append(int(fname.split("_")[1].split(".")[0]))
                except ValueError:
                    pass
    for rn in sorted(rounds, reverse=True):
        if _load_state(rn).get("status") == "completed":
            return rn
    logger.error("❌ Cannot auto-detect round. Use --round N")
    sys.exit(1)


def _resolve_output_dir(round_num):
    state = _load_state(round_num)
    return state.get("round_output", f"./tmp/robot_train_vla_miner/round_{round_num}")


def _metrics(round_num):
    state = _load_state(round_num)
    return state.get("training_metrics", {})


# ─── Step 3: Upload ────────────────────────────────────────

def cmd_upload(args):
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

    from huggingface_hub import HfApi
    api = HfApi(token=hf_token)
    repo_info = api.repo_info(hf_repo_id, repo_type="model")
    hf_commit = repo_info.sha

    state = _load_state(round_num)
    state["hf_repo_id"] = hf_repo_id
    state["hf_url"] = hf_url
    state["hf_commit"] = hf_commit
    state["step"] = "upload"
    state["status"] = "completed"
    if config.hotkey_ss58:
        state["hotkey_ss58"] = config.hotkey_ss58
    _save_state(round_num, state)

    logger.info(f"✅ Uploaded: {hf_url}")
    logger.info(f"   commit: {hf_commit[:8]}")


# ─── Step 4: Burn ──────────────────────────────────────────

def cmd_burn(args):
    config = Config.load(args.config)
    round_num = _resolve_round(args)

    # Fetch control.json for correct burn_rate_tao
    if config.control_json_url:
        control, _ = fetch_control_and_apply(config)
        if control:
            logger.info(f"[rt] burn_rate_tao from control.json: {config.burn_rate_tao}")
        else:
            logger.warning(f"[rt] control.json fetch failed — using default burn_rate_tao={config.burn_rate_tao}")
    else:
        logger.warning(f"[rt] control_json_url not configured — using default burn_rate_tao={config.burn_rate_tao}")

    # Pre-check before spending on burn
    state = _load_state(round_num)
    check_announce_ready(state, round_num)

    logger.info(f"[rt] Step 4/3: Stake Burn Payment | burn_rate_tao={config.burn_rate_tao}")

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

    state = _load_state(round_num)
    state["burn_tx_hash"] = burn_tx
    state["burn_block"] = burn_block
    state["step"] = "burn"
    state["status"] = "completed"
    _save_state(round_num, state)

    logger.info(f"✅ Burn submitted: tx={burn_tx[:16]}... block={burn_block}")


# ─── Step 5: Announce ──────────────────────────────────────

def cmd_announce(args):
    config = Config.load(args.config)
    round_num = _resolve_round(args)

    state = _load_state(round_num)
    metrics = _metrics(round_num)

    hf_repo_id = state.get("hf_repo_id", "")
    hf_url = state.get("hf_url", "")
    burn_tx = state.get("burn_tx_hash", "")
    burn_block = state.get("burn_block", 0)

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

    state["burn_tx_hash"] = burn_tx
    state["step"] = "announce"
    state["status"] = "completed"
    _save_state(round_num, state)


def cmd_submit(args):
    config = Config.load(args.config)
    round_num = _resolve_round(args)
    output_dir = args.output_dir or _resolve_output_dir(round_num)
    metrics = _metrics(round_num)

    logger.info(f"🦞 rt.py submit | round={round_num}")

    # Fetch control.json for correct burn_rate_tao
    if config.control_json_url:
        control, _ = fetch_control_and_apply(config)
        if control:
            logger.info(f"[rt] burn_rate_tao from control.json: {config.burn_rate_tao}")
        else:
            logger.warning(f"[rt] control.json fetch failed — using configured burn_rate_tao={config.burn_rate_tao}")
    else:
        logger.warning(f"[rt] control_json_url not configured — using default burn_rate_tao={config.burn_rate_tao}")

    state = _load_state(round_num)
    if state.get("step") == "announce" and not args.force:
        logger.info("⏭️  Already complete, skipping")
        return

    if args.force:
        logger.info("⚡ --force: re-running full pipeline")

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
        if config.hotkey_ss58:
            state["hotkey_ss58"] = config.hotkey_ss58
        _save_state(round_num, state)
        logger.info(f"✅ Uploaded: {hf_url}")
    else:
        logger.info(f"⏭️  Upload complete: {hf_url}")

    burn_tx = "" if args.force else state.get("burn_tx_hash", "")
    burn_block = state.get("burn_block", 0)
    if not burn_tx:
        # Pre-check before spending on burn
        check_announce_ready(state, round_num)

        burn_rate_tao = getattr(config, "burn_rate_tao", 0.01)
        logger.info(f"[rt] Step 4/3: Stake Burn Payment | burn_rate_tao={burn_rate_tao}")
        password = _read_wallet_password(config)
        subtensor = get_subtensor(config.network)
        wallet = get_wallet(config.coldkey, config.hotkey, config.wallet_path,
                            password=password)
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
    p.add_argument("--round", type=int, default=0)
    p.add_argument("--output-dir", default="")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_submit)

    p = sub.add_parser("upload", help="Step 3: Upload model to HF")
    p.add_argument("--config", default="miner.yaml")
    p.add_argument("--round", type=int, default=0)
    p.add_argument("--output-dir", default="")
    p.set_defaults(func=cmd_upload)

    p = sub.add_parser("burn", help="Step 4: Stake Burn")
    p.add_argument("--config", default="miner.yaml")
    p.add_argument("--round", type=int, default=0)
    p.set_defaults(func=cmd_burn)

    p = sub.add_parser("announce", help="Step 5: Chain Commitment")
    p.add_argument("--config", default="miner.yaml")
    p.add_argument("--round", type=int, default=0)
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
