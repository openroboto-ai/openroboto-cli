"""
miner/push_hf.py — Push trained π₀.₅ model to HuggingFace (public)

Responsibilities:
  1. Push training output (adapter + metrics) to HF repo
  2. Set repo as public
  3. Return HF repo URL
"""

import os
import json
import logging
import threading
from typing import Optional
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

try:
    from huggingface_hub import HfApi, create_repo, upload_folder
    HAS_HF = True
except ImportError:
    HAS_HF = False


def push_model_to_hf(
    model_dir: str,
    repo_id: str,
    hf_token: str,
    round_num: int = 0,
    metrics: Optional[dict] = None,
) -> Optional[str]:
    """将训练好的模型推送到 HuggingFace 公共仓库 / Push trained model to HF public repository.

    Args:
        model_dir: training output dir (contains adapter weights, metrics, etc)
        repo_id: HF repo ID, e.g. "username/pi05-libero-round-1"
        hf_token: HF API token
        round_num: current round number
        metrics: training metrics

    Returns:
        HF repo URL on success, None on failure
    """
    logger.debug(f"[push_model_to_hf] Starting | model_dir={model_dir} repo_id={repo_id} round={round_num}")
    if not HAS_HF:
        logger.error("huggingface_hub not installed. pip install huggingface_hub")
        return None

    if not os.path.isdir(model_dir):
        logger.error(f"Model dir not found: {model_dir}")
        return None

    logger.debug(f"[push_model_to_hf] model_dir exists | files={os.listdir(model_dir)}")
    api = HfApi(token=hf_token)
    logger.debug("[push_model_to_hf] HfApi initialized OK")

    # Write round metadata
    meta = {
        "model": "pi05",
        "config": "pi05_libero",
        "round_num": round_num,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    if metrics:
        meta["metrics"] = {
            "final_loss": metrics.get("final_loss", 0),
            "action_mse": metrics.get("action_mse", 0),
            "training_steps": metrics.get("training_steps", 0),
            "training_duration_sec": metrics.get("training_duration_seconds", 0),
        }
    meta_path = os.path.join(model_dir, "round_info.json")
    with open(meta_path, "w") as f:
        json.dump(meta, f, indent=2)
    logger.debug(f"[push_model_to_hf] round_info.json written: {meta_path}")

    try:
        # Create repo (public), reuse if exists
        logger.info(f"📤 Creating HF repo: {repo_id} (public)")
        create_repo(repo_id=repo_id, token=hf_token, repo_type="model", exist_ok=True)
        logger.debug("[push_model_to_hf] create_repo OK")

        # Calculate directory size
        total_size = 0
        file_count = 0
        for root, dirs, files in os.walk(model_dir):
            for f in files:
                fp = os.path.join(root, f)
                total_size += os.path.getsize(fp)
                file_count += 1
        size_mb = total_size / 1024 / 1024
        logger.info(f"📤 Model dir: {file_count} files, {size_mb:.1f} MB")
        
        # Try setting public (compat old/new API)
        logger.debug(f"[push_model_to_hf] Setting visibility=public")
        try:
            api.update_repo_visibility(repo_id, visibility="public", repo_type="model")
        except AttributeError:
            pass  # Old API lacks this method, create_repo defaults to public
        logger.debug("[push_model_to_hf] Visibility set successfully")

        # Upload entire directory (overwrites existing files)
        logger.info(f"📤 Uploading model to HF: {repo_id}")
        logger.debug(f"[push_model_to_hf] Starting upload_folder | folder_path={model_dir}")
        
        upload_result = [None, None]  # [url, error]
        def _do_upload():
            """在子线程中执行上传 / Execute HF upload in a child thread."""
            try:
                upload_result[0] = upload_folder(
                    folder_path=model_dir,
                    repo_id=repo_id,
                    repo_type="model",
                    token=hf_token,
                    commit_message=f"Round {round_num} π₀.₅ LIBERO model — {datetime.now(timezone.utc).isoformat()}",
                )
                logger.debug(f"[push_model_to_hf] upload_folder returned | url={upload_result[0]}")
            except Exception as e:
                upload_result[1] = e
        
        t = threading.Thread(target=_do_upload, daemon=True)
        t.start()
        t.join(timeout=600)  # 10-minute timeout
        
        if t.is_alive():
            logger.error(f"[push_model_to_hf] upload_folder timed out (600s), possible network issue or large files")
            return None
        if upload_result[1]:
            logger.error(f"[push_model_to_hf] upload_folder exception: {upload_result[1]}")
            raise upload_result[1]
        
        logger.debug(f"[push_model_to_hf] upload_folder OK | url={upload_result[0]}")

        # Prefer commit URL (with SHA), fallback to plain repo URL
        if upload_result[0] and "/commit/" in upload_result[0]:
            hf_url = upload_result[0]  # with commit SHA
        else:
            hf_url = f"https://huggingface.co/{repo_id}"
        logger.info(f"✅ Model pushed to HF: {hf_url}")
        return hf_url

    except Exception as e:
        logger.error(f"[push_model_to_hf] Push failed: {e}", exc_info=True)
        return None
