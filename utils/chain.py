"""
Bittensor chain integration — Chain Announcement + Scoring
"""

import struct
import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Optional, List, Dict

logger = logging.getLogger(__name__)

try:
    import bittensor as bt
    HAS_BT = True
except ImportError:
    HAS_BT = False


# ─── Core Connection ────────────────────────────────────────────

def get_subtensor(network: str = "finney", endpoint: Optional[str] = None):
    """Create a subtensor connection."""
    logger.info(f"[get_subtensor] START | network={network}")
    if not HAS_BT:
        raise ImportError("bittensor is required. pip install bittensor")

    logger.debug(f"[get_subtensor] calling bt.Subtensor(network={network})...")
    sub = bt.Subtensor(network=network)
    logger.debug(f"[get_subtensor] bt.Subtensor() returned OK")
    return sub


def get_wallet(coldkey: str = "default", hotkey: str = "default", path: str = "", password: str = ""):
    """Load a bittensor wallet with optional password.
    
    Args:
        coldkey: Coldkey name.
        hotkey: Hotkey name.
        path: Wallet directory path.
        password: Wallet password to skip interactive prompt.
    """
    coldkey = str(coldkey)
    hotkey = str(hotkey)
    logger.debug(f"[get_wallet] path={path or 'default'} coldkey_name={coldkey} hotkey_name={hotkey}")
    try:
        if path:
            wallet = bt.Wallet(path=str(path), name=coldkey, hotkey=hotkey)
        else:
            wallet = bt.Wallet(name=coldkey, hotkey=hotkey)
    except Exception as e:
        logger.error(f"[get_wallet] wallet load failed: {e}")
        raise

    if not wallet.hotkey_str:
        raise ValueError(f"Hotkey '{hotkey}' is empty or not found in wallet path={path or 'default'}")

    # Set password env var + patch getpass to avoid interactive prompt
    if password:
        os.environ["BT_WALLET_PASSWORD"] = password
        # Bittensor SDK uses getpass.getpass() to read wallet password.
        # Patch it so any call returns our password immediately.
        import getpass as _getpass
        _original_getpass = _getpass.getpass
        _getpass.getpass = lambda prompt="": password
        logger.info(f"[get_wallet] Wallet password set (getpass patched)")

    logger.info(f"[get_wallet] Wallet loaded | hotkey_str={wallet.hotkey_str}")
    return wallet


def get_metagraph(netuid: int, network: str = "finney", subtensor=None):
    logger.debug(f"[get_metagraph] netuid={netuid} network={network}")
    meta = bt.Metagraph(netuid=netuid, network=network, sync=False)
    if subtensor:
        logger.debug("[get_metagraph] syncing...")
        meta.sync(subtensor=subtensor)
        logger.debug(f"[get_metagraph] synced | n={meta.n.item() if hasattr(meta.n, 'item') else meta.n}")
    return meta


# ─── Commitments API ───────────────────────────────

def _submit_commitment(subtensor, wallet, netuid: int, data_bytes: bytes) -> dict:
    """Generic on-chain commitment submission via BigRaw. Returns {ok, extrinsic_hash, block_height, extrinsic_index, extrinsic_ref, fee_tao}."""
    import sys
    data_str = data_bytes.decode("utf-8")
    sys.stdout.write(f"[_submit_commitment] START | netuid={netuid} size={len(data_bytes)} data={data_str}\n")
    sys.stdout.flush()
    return _submit_via_bigraw(subtensor, wallet, netuid, data_bytes)


def _submit_via_bigraw(subtensor, wallet, netuid: int, data_bytes: bytes) -> dict:
    """Submit large payload (>127 bytes) using BigRaw type via publish_metadata_extrinsic."""
    import sys
    try:
        from bittensor.core.extrinsics.serving import publish_metadata_extrinsic
        sys.stdout.write(f"[_submit_via_bigraw] calling publish_metadata_extrinsic(data_type=BigRaw)...\n")
        sys.stdout.flush()
        result = publish_metadata_extrinsic(
            subtensor=subtensor,
            wallet=wallet,
            netuid=netuid,
            data_type="BigRaw",
            data=data_bytes,
            wait_for_inclusion=False,
            wait_for_finalization=False,
        )
        sys.stdout.write(f"[_submit_via_bigraw] returned: type={type(result).__name__} value={repr(result)[:200]}\n")
        sys.stdout.flush()
        return _parse_result(result, subtensor)
    except Exception as e:
        sys.stdout.write(f"[_submit_via_bigraw] FAIL: {type(e).__name__}: {e}\n")
        sys.stdout.flush()
        return {"ok": False, "extrinsic_hash": "", "block_height": 0, "extrinsic_index": 0, "extrinsic_ref": "", "fee_tao": 0.0}


def _parse_result(result, subtensor) -> dict:
    """Parse ExtrinsicResponse result into a dict."""
    extrinsic_hash = ""
    block_height = 0
    extrinsic_index = 0
    fee_tao = 0.0
    if result:
        if hasattr(result, 'extrinsic_hash'):
            try:
                extrinsic_hash = result.extrinsic_hash.hex() if isinstance(result.extrinsic_hash, bytes) else str(result.extrinsic_hash)
            except Exception:
                extrinsic_hash = str(getattr(result, 'extrinsic_hash', ''))
        if hasattr(result, 'extrinsic_receipt') and result.extrinsic_receipt:
            receipt = result.extrinsic_receipt
            try:
                if extrinsic_hash == "" or extrinsic_hash == "0x":
                    extrinsic_hash = receipt.extrinsic_hash.hex() if isinstance(receipt.extrinsic_hash, bytes) else str(receipt.extrinsic_hash)
            except Exception:
                pass
            try:
                block_height = getattr(receipt, 'block_number', 0)
            except Exception:
                pass
            try:
                extrinsic_index = getattr(receipt, 'extrinsic_idx', 0)
            except Exception:
                pass
        if hasattr(result, 'extrinsic_fee') and result.extrinsic_fee:
            fee_tao = result.extrinsic_fee.tao
        elif hasattr(result, 'transaction_tao_fee') and result.transaction_tao_fee:
            fee_tao = result.transaction_tao_fee.tao

    # Fallback to current block number
    try:
        if block_height == 0:
            block_height = subtensor.get_current_block()
    except Exception:
        pass

    success = getattr(result, 'is_success', False) or getattr(result, 'success', False) or result is True

    ref = f"{block_height}-{extrinsic_index}" if block_height else "N/A"
    fee_str = f"{fee_tao*1e9:.2f} nanoTAO ({fee_tao:.6f} TAO)" if fee_tao else "N/A"
    sys.stdout.write(f"[_parse_result] success={success} ref={ref} hash=0x{extrinsic_hash[:16] if extrinsic_hash else 'N/A'}... fee={fee_str}\n")
    sys.stdout.flush()
    return {"ok": success, "extrinsic_hash": extrinsic_hash, "block_height": block_height,
            "extrinsic_index": extrinsic_index, "extrinsic_ref": ref, "fee_tao": fee_tao}


def _read_commitment(subtensor, netuid: int, hotkey_ss58: str) -> Optional[dict]:
    """Read a commitment from chain."""
    logger.debug(f"[_read_commitment] START | netuid={netuid} hotkey={hotkey_ss58[:10]}...")
    if not HAS_BT:
        return None
    try:
        if hasattr(subtensor, 'get_commitment_metadata'):
            logger.debug("[_read_commitment] calling get_commitment_metadata")
            raw = subtensor.get_commitment_metadata(netuid=netuid, hotkey_ss58=hotkey_ss58)
            logger.debug(f"[_read_commitment] raw type={type(raw).__name__} raw={raw}")
            if raw:
                decoded = _decode_raw(raw)
                logger.debug(f"[_read_commitment] decoded: {decoded}")
                return decoded
    except Exception as e:
        logger.debug(f"[_read_commitment] exception: {e}")
    logger.debug("[_read_commitment] returning None")
    return None


def _decode_raw(raw) -> Optional[dict]:
    """Decode on-chain raw data into a dict.
    
    bittensor 10.x get_commitment_metadata returns:
    {'deposit': 0, 'block': 7496925, 'info': {'fields': (({'Raw73': ((123, 34, 104, ...)},),)}}}
    """
    logger.debug(f"[_decode_raw] START | type={type(raw).__name__}")
    try:
        if isinstance(raw, dict):
            info = raw.get('info', {})
            fields = info.get('fields', [])
            if fields:
                for field_group in fields:
                    if isinstance(field_group, (list, tuple)):
                        for field in field_group:
                            if isinstance(field, dict):
                                for key, val in field.items():
                                    if key.startswith('Raw') and val:
                                        if isinstance(val, tuple):
                                            # Handle nested tuples: ((123, 34, ...),)
                                            flat = []
                                            for item in val:
                                                if isinstance(item, tuple):
                                                    flat.extend(list(item))
                                                elif isinstance(item, int):
                                                    flat.append(item)
                                            byte_data = bytes(flat)
                                        elif isinstance(val, bytes):
                                            byte_data = val
                                        else:
                                            continue
                                        decoded = json.loads(byte_data.decode('utf-8'))
                                        logger.debug(f"[_decode_raw] OK from Raw dict | keys={list(decoded.keys())}")
                                        return decoded
            if 'info' not in raw and 'fields' not in raw:
                logger.debug(f"[_decode_raw] returning raw dict | keys={list(raw.keys())}")
                return raw
        elif isinstance(raw, bytes):
            decoded = json.loads(raw.decode("utf-8"))
            logger.debug(f"[_decode_raw] OK bytes | keys={list(decoded.keys())}")
            return decoded
        elif isinstance(raw, str):
            decoded = json.loads(raw)
            logger.debug(f"[_decode_raw] OK str | keys={list(decoded.keys())}")
            return decoded
    except Exception as e:
        logger.debug(f"[_decode_raw] decode failed: {e}")
    logger.debug("[_decode_raw] returning None")
    return None


# ─── Miner: HF Model Submission to Chain ─────────────────────────────

def submit_hf_model_announcement(
    subtensor,
    wallet,
    netuid: int,
    hf_repo_id: str,
    hf_url: str,
    round_num: int,
    metrics: Optional[dict] = None,
    burn_tx_hash: str = "",
    burn_block: int = 0,
    block_hash: str = "",
    hotkey_ss58: str = "",
) -> dict:
    """Submit HF model address to chain after training completes. Returns {ok, extrinsic_hash, block_height, extrinsic_ref, fee_tao}."""
    logger.info(f"[submit_hf_model_announcement] START | repo={hf_repo_id} round={round_num}")

    # Auto-resolve SS58 from wallet hotkey if not provided
    if not hotkey_ss58:
        hotkey_ss58 = wallet.hotkey.ss58_address
        logger.info(f"[submit_hf_model_announcement] Resolved hotkey SS58: {hotkey_ss58[:16]}...")

    hf_commit = ""
    if "/commit/" in hf_url:
        hf_commit = hf_url.split("/commit/")[1][:40]

    # Store all fields in full JSON format. Fits within Data::BigRaw (512 bytes limit).
    # s = hotkey SS58 address (full)
    # h = block_hash (full, no 0x prefix)
    # c = hf_commit (full, 40 hex chars)
    # r = round_num
    # i = hf_repo_id (full)
    # b = burn_tx_hash (full, no 0x prefix)
    # bb = burn_block_number (integer)
    data = {
        "s": hotkey_ss58,
        "h": block_hash[2:] if block_hash.startswith("0x") else block_hash,
        "c": hf_commit,
        "r": round_num,
        "i": hf_repo_id,
        "b": burn_tx_hash[2:] if burn_tx_hash.startswith("0x") else burn_tx_hash,
        "bb": burn_block if burn_block else None,
    }

    data_str = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    data_bytes = data_str.encode("utf-8")

    logger.info(f"📡 HF Model on-chain: {hf_repo_id} commit={hf_commit[:8] if hf_commit else '?'} "
                f"data_size={len(data_bytes)} bytes (BigRaw limit, full JSON)")

    result = _submit_commitment(subtensor, wallet, netuid, data_bytes)
    sys.stdout.write(f"[submit_hf_model_announcement] _submit_commitment returned ok={result.get('ok')}\n")
    sys.stdout.flush()

    # If submission succeeded, return immediately — verification is fire-and-forget
    if result.get("ok"):
        fee = result.get("fee_tao", 0)
        fee_str = f"{fee*1e9:.2f} nanoTAO ({fee:.6f} TAO)" if fee else "N/A"
        ext_hash = result.get("extrinsic_hash", "")
        ref = result.get("extrinsic_ref", "N/A")
        sys.stdout.write(f"[submit_hf_model_announcement] ✅ HF Model on-chain | ref={ref} hash=0x{ext_hash[:16] if ext_hash else 'N/A'} fee={fee_str}\n")
        sys.stdout.flush()
        return result

    sys.stdout.write(f"[submit_hf_model_announcement] Submission failed\n")
    sys.stdout.flush()
    return result


# ─── Validator: Benchmark Score to Chain ──────────────────────────

def submit_benchmark_score(
    subtensor,
    wallet,
    netuid: int,
    miner_hotkey: str,
    round_num: int,
    hf_repo_id: str,
    benchmark_result: dict,
) -> dict:
    """Submit validator benchmark score to chain."""
    logger.debug(f"[submit_benchmark_score] START | miner={miner_hotkey[:10]}... round={round_num}")
    if not HAS_BT:
        return {"ok": False, "extrinsic_hash": "", "block_height": 0, "extrinsic_index": 0, "extrinsic_ref": "", "fee_tao": 0.0}

    data = {
        "type": "benchmark_score",
        "miner_hotkey": miner_hotkey,
        "round_num": round_num,
        "hf_repo_id": hf_repo_id,
        "action_accuracy": benchmark_result.get("action_accuracy", 0),
        "task_success": benchmark_result.get("task_success", 0),
        "trajectory_smoothness": benchmark_result.get("trajectory_smoothness", 0),
        "efficiency": benchmark_result.get("efficiency", 0),
        "total_score": benchmark_result.get("total_score", 0),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    data_bytes = json.dumps(data, ensure_ascii=False).encode("utf-8")

    result = _submit_commitment(subtensor, wallet, netuid, data_bytes)
    if result.get("ok"):
        fee = result.get("fee_tao", 0)
        fee_str = f"{fee*1e9:.2f} nanoTAO ({fee:.6f} TAO)" if fee else "N/A"
        ref = result.get("extrinsic_ref", "N/A")
        logger.info(f"✅ Benchmark score on-chain | ref={ref} fee={fee_str}")
    else:
        logger.warning("[submit_benchmark_score] Submission failed")
    return result


# ─── Chain Data Reading ─────────────────────────────────────────

def scan_chain_submissions(
    subtensor,
    netuid: int,
    network: str = "test",
    round_num: Optional[int] = None,
    max_age_hours: float = 48.0,
    hf_username: str = "",
) -> List[dict]:
    """Scan all miner HF model submissions on chain."""
    logger.debug(f"[scan_chain_submissions] START | netuid={netuid} network={network} round={round_num}")
    if not HAS_BT:
        return []

    try:
        metagraph = get_metagraph(netuid, network=network, subtensor=subtensor)
        logger.debug(f"[scan_chain_submissions] metagraph has {len(metagraph.hotkeys)} hotkeys")

        submissions = []
        for uid in range(len(metagraph.hotkeys)):
            hotkey = metagraph.hotkeys[uid]
            raw_data = _read_commitment(subtensor, netuid, hotkey)
            if not raw_data:
                continue

            committed_hotkey = raw_data.get("s", "")
            commit_round = raw_data.get("r", 0)
            hf_commit = raw_data.get("c", "")

            # Verify hotkey matches the full SS58 stored in 's'
            if committed_hotkey and committed_hotkey != hotkey:
                continue
            if round_num is not None and commit_round != round_num:
                continue

            full_ss58 = hotkey
            hf_repo_id = f"{hf_username}/pi05-{full_ss58}" if hf_username else f"pi05-{full_ss58}"
            hf_url = f"https://huggingface.co/{hf_repo_id}"

            submissions.append({
                "uid": uid,
                "hotkey": hotkey,
                "hotkey_short": hotkey[:6] + "..." + hotkey[-4:],
                "hf_repo_id": hf_repo_id,
                "hf_url": hf_url,
                "hf_commit": hf_commit,
                "round_num": commit_round,
                "stake": float(metagraph.S[uid]),
                "active": bool(metagraph.active[uid]) if hasattr(metagraph, 'active') else True,
            })

        logger.debug(f"[scan_chain_submissions] OK | found={len(submissions)}")
        return submissions

    except Exception as e:
        logger.error(f"[scan_chain_submissions] FAIL: {e}")
        return []


def scan_chain_scores(
    subtensor,
    netuid: int,
    network: str = "test",
    round_num: Optional[int] = None,
    max_age_hours: float = 48.0,
) -> List[dict]:
    """Scan all validator benchmark scores on chain."""
    logger.debug(f"[scan_chain_scores] START | netuid={netuid} network={network}")
    if not HAS_BT:
        return []

    try:
        metagraph = get_metagraph(netuid, network=network, subtensor=subtensor)
        scores = []
        for uid in range(len(metagraph.hotkeys)):
            hotkey = metagraph.hotkeys[uid]
            raw_data = _read_commitment(subtensor, netuid, hotkey)
            if not raw_data:
                continue

            if raw_data.get("type") != "benchmark_score":
                continue

            scores.append({
                "uid": uid,
                "validator_hotkey": hotkey,
                "miner_hotkey": raw_data.get("miner_hotkey", ""),
                "round_num": raw_data.get("round_num", 0),
                "total_score": raw_data.get("total_score", 0),
                "stake": float(metagraph.S[uid]),
            })

        logger.debug(f"[scan_chain_scores] OK | found={len(scores)}")
        return scores

    except Exception as e:
        logger.error(f"[scan_chain_scores] FAIL: {e}")
        return []


def get_chain_weights(
    subtensor,
    netuid: int,
    network: str = "test",
) -> list[dict]:
    """Read current weights from chain."""
    logger.debug(f"[get_chain_weights] START | netuid={netuid}")
    if not HAS_BT:
        return []

    try:
        metagraph = get_metagraph(netuid, network=network, subtensor=subtensor)
        results = []
        if hasattr(metagraph, 'weights') and metagraph.weights is not None:
            for entry in metagraph.weights:
                if len(entry) >= 2:
                    w_uid = int(entry[0])
                    w_u16 = int(entry[1])
                    results.append({
                        "uid": w_uid,
                        "hotkey": metagraph.hotkeys[w_uid] if w_uid < len(metagraph.hotkeys) else "?",
                        "weight_u16": w_u16,
                        "weight_pct": round(w_u16 / 65535 * 100, 2),
                    })
        logger.debug(f"[get_chain_weights] OK | found={len(results)}")
        return sorted(results, key=lambda x: x["weight_u16"], reverse=True)

    except Exception as e:
        logger.error(f"[get_chain_weights] FAIL: {e}")
        return []


# ─── Utilities ────────────────────────────────────────────────

def _short_key(key: str, prefix: int = 6, suffix: int = 4) -> str:
    if len(key) <= prefix + suffix:
        return key
    return f"{key[:prefix]}...{key[-suffix:]}"
