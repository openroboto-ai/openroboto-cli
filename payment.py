"""
Miner Payment — Stake Burn on Bittensor Chain

Uses the chain's `add_stake_burn` extrinsic to permanently burn TAO
as proof of payment. The resulting extrinsic hash serves as payment
credential for backend verification.

Usage (miner side, after training completes):
    from payment import execute_stake_burn

    tx_hash = execute_stake_burn(
        subtensor=sub,
        wallet=wallet,
        netuid=313,
        amount_tao=0.01,
    )
"""

import json
import logging
import os
import asyncio
from backend.logging_setup import setup_module_logger
from typing import Optional

logger = setup_module_logger(__name__)

try:
    import bittensor as bt
    HAS_BT = True
except ImportError:
    HAS_BT = False


def _lookup_tx_block(subtensor, tx_hash: str, max_retries: int = 5, wait_sec: float = 2.0) -> int:
    """Look up block number for a tx_hash by scanning recent blocks.
    Used when the v11 SDK doesn't return block_number directly.
    Retries with a short wait because the tx may still be propagating."""
    import time
    tx_hash_clean = tx_hash.replace("0x", "")
    for attempt in range(max_retries):
        try:
            current = subtensor.substrate.get_block()
            if not current:
                time.sleep(wait_sec)
                continue
            current_num = current["header"]["number"]
            for bn in range(current_num, max(0, current_num - 15), -1):
                block = subtensor.substrate.get_block(block_number=bn)
                if not block:
                    continue
                for ext in block.get("extrinsics", []):
                    ext_h = ext.get("extrinsic_hash", "")
                    if ext_h == tx_hash or ext_h == tx_hash_clean or ext_h == "0x" + tx_hash_clean:
                        return bn
            if attempt < max_retries - 1:
                time.sleep(wait_sec)
        except Exception:
            if attempt < max_retries - 1:
                time.sleep(wait_sec)
    return 0


def execute_stake_burn(
    subtensor,
    wallet,
    netuid: int,
    amount_tao: float,
    hotkey_ss58: Optional[str] = None,
    limit_price_rao: int = 0,
) -> Optional[str]:
    """
    Execute add_stake_burn on the Bittensor chain.

    Args:
        subtensor: bt.Subtensor instance
        wallet: bt.Wallet (coldkey + hotkey)
        netuid: target subnet
        amount_tao: TAO to burn
        hotkey_ss58: optional hotkey override (defaults to wallet.hotkey)
        limit_price_rao: worst acceptable price in rao per alpha (0 = market)

    Returns:
        dict with keys:
        - tx_hash: hex string (or None)
        - block_number: int (or None)
    """
    if not HAS_BT:
        logger.error("bittensor not available — cannot execute stake burn")
        return None

    hk = hotkey_ss58 if hotkey_ss58 else wallet.hotkey.ss58_address

    logger.info(
        f"🔥 Executing stake burn: {amount_tao} TAO on netuid={netuid} "
        f"via hotkey {hk[:8]}..."
    )

    try:
        # Always use v10 raw extrinsic — waits for inclusion and returns block_number
        return _sync_execute_fallback(
            subtensor, wallet, netuid, amount_tao, hk,
            limit_price_rao=limit_price_rao,
        )

    except Exception as e:
        logger.error(f"❌ Stake burn exception: {e}")
        import traceback
        traceback.print_exc()
        return None


async def _async_execute(intent, wallet) -> dict:
    """Execute stake burn using v11 async Client."""
    try:
        async with bt.Client("finney") as client:
            result = await client.execute(intent, wallet)
            return {
                "success": getattr(result, "success", False),
                "extrinsic_hash": getattr(result, "extrinsic_hash", None),
                "block_hash": getattr(result, "block_hash", None),
                "error": getattr(getattr(result, "error", None), "__dict__", {}),
            }
    except Exception as e:
        return {
            "success": False,
            "error": {"code": "exec_error", "remediation": str(e)},
        }


def _sync_execute_fallback(
    subtensor, wallet, netuid: int, amount_tao: float, hotkey_ss58: str,
    limit_price_rao: int = 0,
) -> Optional[str]:
    """
    Fallback: use v10 subtensor raw extrinsic to call add_stake_burn.
    """
    try:
        logger.info("🔧 Using v10 fallback (raw extrinsic)...")

        call = subtensor.substrate.compose_call(
            call_module="SubtensorModule",
            call_function="add_stake_burn",
            call_params={
                "netuid": netuid,
                "amount": int(amount_tao * 1e9),  # Convert TAO to Rao
                "hotkey": hotkey_ss58,
                "limit": limit_price_rao if limit_price_rao > 0 else 0xFFFFFFFFFFFFFFFF,  # max u64 = accept market price
            },
        )

        extrinsic = subtensor.substrate.create_signed_extrinsic(
            call=call,
            keypair=wallet.coldkey,
        )

        receipt = subtensor.substrate.submit_extrinsic(
            extrinsic,
            wait_for_inclusion=True,
            wait_for_finalization=False,
        )

        if receipt.is_success:
            tx_hash = receipt.extrinsic_hash
            block_number = receipt.block_number
            # SDK may not populate block_number even with wait_for_inclusion=True
            # Use block_hash to resolve the block number
            if block_number is None and getattr(receipt, 'block_hash', None):
                try:
                    block_number = subtensor.substrate.get_block_number(receipt.block_hash)
                except Exception:
                    block_number = _lookup_tx_block(subtensor, tx_hash)
            # Last resort: scan recent blocks
            if block_number is None:
                block_number = _lookup_tx_block(subtensor, tx_hash)
            logger.success(
                f"✅ Stake burn submitted (v10 fallback) | "
                f"tx_hash={tx_hash[:16]}... block={block_number}"
            )
            return {"tx_hash": tx_hash, "block_number": block_number}
        else:
            err_msg = receipt.error_message
            logger.error(f"❌ Stake burn failed (v10): {err_msg}")
            return None

    except Exception as e:
        logger.error(f"❌ v10 fallback also failed: {e}")
        import traceback
        traceback.print_exc()
        return None


def verify_burn_on_chain(
    subtensor,
    netuid: int,
    tx_hash: str,
    hotkey: str,
    expected_amount_tao: float,
    burn_block: int = 0,
) -> dict:
    """
    Verify burn tx at the reported block.
    
    1. tx_hash exists in burn_block
    2. signer matches hotkey or coldkey owns the hotkey
    3. burn amount matches expected_amount_tao
    """
    try:
        import bittensor as bt
    except ImportError:
        logger.error("bittensor not available — cannot verify burn on chain")
        return {"verified": False, "error": "bittensor not available"}

    try:
        if burn_block <= 0:
            return {"verified": False, "error": "burn_block not provided"}

        logger.info(f"[verify] checking tx={tx_hash[:16]}... at burn_block={burn_block}")
        block = subtensor.substrate.get_block(block_number=burn_block)
        if not block:
            return {"verified": False, "error": f"block {burn_block} not found"}

        tx_hash_clean = tx_hash.replace("0x", "").lower()
        found_ext = None
        for extrinsic in block.get("extrinsics", []):
            ext_hash = getattr(extrinsic, "extrinsic_hash", None)
            if ext_hash:
                ext_hash = ext_hash.hex().lower() if isinstance(ext_hash, bytes) else str(ext_hash).lower().replace("0x", "")
            if ext_hash and ext_hash == tx_hash_clean:
                found_ext = extrinsic
                break

        if not found_ext:
            return {"verified": False, "error": f"tx not found at block {burn_block}"}

        # Get signer from value_object
        value_obj = getattr(found_ext, "value_object", None) or {}
        signer = ""
        if isinstance(value_obj, dict):
            addr = value_obj.get("address") or value_obj.get("signer")
            if addr is not None:
                signer = getattr(addr, "value", None) or str(addr)
        if not signer:
            signer = getattr(found_ext, "address", "") or ""

        # Signer can be coldkey — check if this coldkey owns the hotkey
        if signer and signer != hotkey:
            try:
                owner = subtensor.get_hotkey_owner(hotkey)
                if owner != signer:
                    return {"verified": False, "error": f"signer mismatch: {signer[:16]}..."}
            except Exception:
                return {"verified": False, "error": f"signer mismatch: {signer[:16]}..."}

        # Extract burn amount and target hotkey from extrinsic value dict
        ext_value = getattr(found_ext, "value", {})
        call_params = {}
        if isinstance(ext_value, dict):
            raw_call = ext_value.get("call", {})
            if isinstance(raw_call, dict):
                call_params = raw_call
            elif hasattr(raw_call, "value") and isinstance(getattr(raw_call, "value", None), dict):
                call_params = raw_call.value

        # call_params might be a dict with 'call_args': [{name, type, value}, ...]
        # or a flat dict with keys like 'amount', 'hotkey', etc.
        burn_amount_rao = call_params.get("amount")
        burn_target_hotkey = call_params.get("hotkey")

        # If not found directly, extract from call_args list
        if burn_amount_rao is None or burn_target_hotkey is None:
            call_args = call_params.get("call_args", [])
            if isinstance(call_args, list):
                for arg in call_args:
                    if isinstance(arg, dict):
                        name = arg.get("name")
                        value = arg.get("value")
                        if name == "amount":
                            burn_amount_rao = value
                        elif name == "hotkey":
                            burn_target_hotkey = value

        # Amount check (must be at least expected amount)
        expected_rao = int(expected_amount_tao * 1e9)
        if burn_amount_rao is None:
            return {"verified": False, "error": "amount not found in tx"}
        if burn_amount_rao < expected_rao:
            return {"verified": False, "error": f"amount insufficient: got {burn_amount_rao}, expected >= {expected_rao}"}

        # Target hotkey check (mandatory)
        if burn_target_hotkey is None:
            return {"verified": False, "error": "hotkey param not found in tx"}
        if burn_target_hotkey != hotkey:
            return {"verified": False, "error": f"burn target mismatch: tx targets {burn_target_hotkey[:16]}..., not {hotkey[:16]}..."}

        logger.info(
            f"✅ Chain verified: tx={tx_hash[:16]}... | "
            f"block={burn_block} signer={signer[:16]}... amount={burn_amount_rao}"
        )
        return {"verified": True, "signer": signer, "block_number": burn_block, "timestamp": ""}

    except Exception as e:
        logger.warning(f"❌ Chain verification failed for tx={tx_hash[:16]}...: {e}")
        return {"verified": False, "error": str(e)}


