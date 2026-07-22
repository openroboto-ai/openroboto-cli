"""
Validator — Weight Setter

Inspired by Affine design:
  - Validator no longer runs benchmarks, only reads weights from backend API
  - Read weights → set on chain
  - Supports single-run and continuous service modes
"""

import argparse
import json
import logging
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.config import Config
from utils.logger import setup_logger
from utils.chain import get_subtensor, get_wallet, scan_chain_submissions

logger = setup_logger("validator", "INFO")


def fetch_control(control_url: str, last_etag: str = "") -> tuple:
    """通过 HTTP 下载 control.json / Download control.json via HTTP; return (None, etag) if ETag unchanged."""
    try:
        req = urllib.request.Request(control_url)
        req.add_header("User-Agent", "robot-train-subnet/0.5")
        if last_etag:
            req.add_header("If-None-Match", last_etag)
        resp = urllib.request.urlopen(req, timeout=30)
        etag = resp.headers.get("ETag", "").strip('"')
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


def fetch_weights(backend_url: str, public_key: str = "") -> dict:
    """
    从后端 API 获取当前权重。
    Fetch current weights from the backend API.

    Args:
        backend_url: 后端 API 地址。
        public_key: public API key (from control.json → public_keys).

    Returns:
        权重字典，失败时返回空字典。
    """
    try:
        import urllib.request
        req = urllib.request.Request(f"{backend_url}/api/weights")
        if public_key:
            req.add_header("X-API-Key", public_key)
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
            logger.info(f"[fetch_weights] API response: {json.dumps(data)[:300]}")
            return data
    except Exception as e:
        logger.error(f"[fetch_weights] Failed to fetch weights from {backend_url}: {e}")
        return {}


def normalize_weights(weights_raw: dict, uids: list[str]) -> tuple:
    """
    将原始权重归一化为 u16 [0, 65535] 范围，仅保留正值。
    Normalize weights to u16 [0, 65535] range, keeping only positive values.

    Args:
        weights_raw: 原始权重字典 {hotkey: weight}。
        uids: 有序的热键列表。

    Returns:
        (uid_list, weight_list, detail_log) — UID 列表、归一化后的 u16 权重列表、详细日志。
    """
    uid_list = []
    weight_list = []
    detail_lines = []

    # Step 1: collect positive weights
    positive = {}
    for i, hk in enumerate(uids):
        w = weights_raw.get(hk, 0.0)
        if w > 0:
            positive[i] = w
            detail_lines.append(f"  uid={i:3d} hotkey={hk[:12]}... raw={w:.6f}")

    if not positive:
        return [], [], ["No positive weights"]

    # Step 2: normalize to sum=1.0
    total = sum(positive.values())
    normed = {idx: w / total for idx, w in positive.items()}

    # Step 3: convert to u16
    uid_list = list(normed.keys())
    weight_list = [int(w * 65535) for w in normed.values()]

    detail_lines.append(f"  Total raw={total:.6f}, normalized to sum=1.0")
    for idx, w in zip(uid_list, weight_list):
        detail_lines.append(f"  → uid={idx:3d} u16={w:5d} ({normed[idx]:.6f})")

    return uid_list, weight_list, detail_lines


def set_weights_on_chain(subtensor, wallet, netuid: int, weights: dict, uids: list[str]) -> bool:
    """
    将权重设置到链上。
    Set weights on chain.

    Args:
        subtensor: Subtensor 实例。
        wallet: 钱包实例。
        netuid: 网络 UID。
        weights: 权重字典 {hotkey: weight}。
        uids: 有序的热键列表。

    Returns:
        设置成功返回 True，否则返回 False。
    """
    try:
        logger.info(f"[set_weights] Total miners in metagraph: {len(uids)}")
        logger.info(f"[set_weights] Raw weights from API:")
        uid_list, weight_list, detail_log = normalize_weights(weights, uids)
        for line in detail_log:
            logger.info(f"[set_weights] {line}")

        if not uid_list:
            logger.warning("[set_weights] No positive weights to set")
            return False

        logger.info(f"[set_weights] Setting {len(uid_list)} non-zero weights on chain (netuid={netuid})...")

        if hasattr(subtensor, 'set_weights'):
            result = subtensor.set_weights(
                wallet=wallet,
                netuid=netuid,
                uids=uid_list,
                weights=weight_list,
            )
            if result:
                success = getattr(result, 'is_success', False) or result is True
                extrinsic_hash = ""
                if hasattr(result, 'extrinsic_hash'):
                    try:
                        extrinsic_hash = result.extrinsic_hash.hex() if isinstance(result.extrinsic_hash, bytes) else str(result.extrinsic_hash)[:16]
                    except Exception:
                        pass
                if success:
                    logger.info(f"[set_weights] ✅ Chain set_weights OK | success={success} hash=0x{extrinsic_hash if extrinsic_hash else 'N/A'}")
                    return True
                else:
                    # result is truthy but is_success is False → actual chain failure
                    msg = getattr(result, 'status_message', '') or str(result)
                    logger.error(f"[set_weights] ❌ Chain set_weights FAILED (result truthy but is_success=False) | hash=0x{extrinsic_hash if extrinsic_hash else 'N/A'} | msg={msg}")
                    return False
            else:
                logger.warning("[set_weights] set_weights returned False")
                return False
        else:
            logger.error("[set_weights] subtensor.set_weights not available")
            return False

    except Exception as e:
        logger.error(f"[set_weights] Failed: {type(e).__name__}: {e}", exc_info=True)
        return False


def run_validator(config_path: str = "validator.yaml", service_mode: bool = False):
    """
    运行验证器：监听链上矿工提交并从后端读取权重设置到链上。
    Run validator: listen to on-chain miner submissions and set backend weights on chain.

    Args:
        config_path: 配置文件路径，默认为 validator.yaml。
        service_mode: True 为持续服务模式，False 为单次运行后退出。
    """
    config = Config.load(config_path)
    backend_url = config.backend_url
    control_url = config.control_json_url

    # Weight setting interval (minutes), read from config, default 12h = 720min
    weight_interval_min = getattr(config, "weight_interval_min", 720)
    weight_interval = weight_interval_min * 60  # Convert to seconds
    poll_interval = 60     # Scan chain + refresh control.json every 60s

    logger.info(f"Validator started")
    logger.info(f"  Network: {config.network}")
    logger.info(f"  Netuid: {config.netuid}")
    logger.info(f"  Backend: {backend_url}")
    logger.info(f"  control.json: {control_url}")
    logger.info(f"  Weight interval: {weight_interval_min}min")
    logger.info(f"  Service mode: {service_mode}")

    subtensor = get_subtensor(config.network)
    wallet = get_wallet(config.coldkey, config.hotkey, config.wallet_path)

    last_weight_set = 0
    control_etag = ""  # ETag cache for control.json
    public_key = getattr(config, 'backend_public_key', '')  # Initial key from config

    while True:
        try:
            # ── 1. Refresh control.json → update public_key ──────────────────
            if control_url:
                ctrl, new_etag = fetch_control(control_url, control_etag)
                if ctrl is not None:
                    control_etag = new_etag
                    # Update public_key from control.json
                    pk = ctrl.get("public_key", "")
                    if isinstance(pk, str) and pk:
                        if pk != public_key:
                            logger.info(f"[control] public_key updated: '{pk[:8]}...'")
                            public_key = pk
                    # Also apply other control.json config if needed
                    config.apply_control(ctrl)

            # ── 2. Scan on-chain miner submissions ──────────────────────────
            scan_chain_submissions(
                subtensor, config.netuid, network=config.network,
            )

            # ── 3. Read backend weights → set on chain ────────────────
            now = time.time()
            if now - last_weight_set >= weight_interval:
                weights = fetch_weights(backend_url, public_key)
                if weights:
                    from utils.chain import get_metagraph
                    metagraph = get_metagraph(config.netuid, network=config.network, subtensor=subtensor)
                    uids = metagraph.hotkeys

                    ok = set_weights_on_chain(subtensor, wallet, config.netuid, weights, uids)
                    if ok:
                        logger.info(f"✅ Weight set complete at {datetime.now(timezone.utc).isoformat()}")
                        last_weight_set = now
                    else:
                        logger.warning("Weight set returned False")
                else:
                    logger.warning("No weights from backend")

        except Exception as e:
            logger.error(f"Validator run failed: {e}")

        logger.info(f"Next scan in {poll_interval}s | Next weight set in {weight_interval_min}min...")
        time.sleep(poll_interval)


def main():
    """
    程序入口：解析命令行参数并启动验证器。
    Entry point: parse CLI arguments and start the validator.
    """
    parser = argparse.ArgumentParser(description="Validator — weight setter")
    parser.add_argument("--config", type=str, default="validator.yaml")
    args = parser.parse_args()
    run_validator(args.config, service_mode=True)


if __name__ == "__main__":
    main()
