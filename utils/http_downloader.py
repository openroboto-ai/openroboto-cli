"""
HTTP Downloader — replaces R2 bucket approach

All resources downloaded via public HTTP URLs, no credentials needed.
Public resource URLs are provided through local config or control.json.

Design principles:
  - Each download item = one independent HTTP URL
  - URLs can be saved to config files for easy debugging and sharing
  - Supports ETag caching, retries, and checksum verification
"""

import os
import json
import time
import logging
import hashlib
import urllib.request
from typing import Optional

logger = logging.getLogger(__name__)


def download_file(
    url: str,
    local_path: str,
    max_retries: int = 3,
    timeout_sec: int = 300,
) -> str:
    """
    Download a file via HTTP GET with retry support.

    Args:
        url: Public HTTP URL
        local_path: Local save path
        max_retries: Maximum retries
        timeout_sec: Timeout in seconds

    Returns:
        local_path
    """
    logger.debug(f"[download_file] Starting | url={url} path={local_path} retries={max_retries}")
    os.makedirs(os.path.dirname(local_path) or ".", exist_ok=True)

    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            logger.debug(f"[download_file] Attempt {attempt}/{max_retries}")
            req = urllib.request.Request(url)
            req.add_header("User-Agent", "robot-train-subnet/0.5")

            with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
                size = resp.headers.get("Content-Length")
                logger.debug(f"[download_file] HTTP {resp.status} | Content-Length={size or 'unknown'}")
                if size:
                    logger.info(f"   Size: {int(size) / 1024 / 1024:.1f} MB")

                with open(local_path, "wb") as f:
                    while True:
                        chunk = resp.read(65536)
                        if not chunk:
                            break
                        f.write(chunk)

                file_size = os.path.getsize(local_path)
                logger.info(f"✅ Downloaded: {local_path} ({file_size} bytes)")
                return local_path

        except Exception as e:
            last_err = e
            if attempt < max_retries:
                delay = min(2 ** attempt, 30)
                logger.warning(f"   Download failed (attempt {attempt}/{max_retries}): {e}")
                logger.debug(f"[download_file] Waiting {delay}s s before retry")
                time.sleep(delay)
            else:
                logger.error(f"   Download failed after {max_retries} retries):: {e}")

    raise last_err


def download_json(url: str, max_retries: int = 3) -> dict:
    """Download and parse JSON."""
    import tempfile
    logger.debug(f"[download_json] url={url}")
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
        download_file(url, tmp.name, max_retries)
        with open(tmp.name, "r") as f:
            data = json.load(f)
        logger.debug(f"[download_json] Parsed OK | keys={list(data.keys()) if isinstance(data, dict) else type(data).__name__}")
        os.unlink(tmp.name)
        return data


def download_file_with_etag(
    url: str,
    local_path: str,
    etag_path: Optional[str] = None,
) -> tuple:
    """
    Download with ETag caching.

    Skip if ETag unchanged, returns (False, etag).
    Otherwise download new file, returns (True, etag).

    Returns:
        (updated: bool, etag: str)
    """
    if not etag_path:
        etag_path = local_path + ".etag"

    # Read previous ETag
    last_etag = ""
    if os.path.exists(etag_path):
        with open(etag_path) as f:
            last_etag = f.read().strip()

    try:
        req = urllib.request.Request(url)
        req.add_header("User-Agent", "robot-train-subnet/0.5")
        if last_etag:
            req.add_header("If-None-Match", last_etag)

        with urllib.request.urlopen(req, timeout=60) as resp:
            if resp.status == 304:
                logger.debug(f"ETag unchanged: {url}")
                return False, last_etag

            new_etag = resp.headers.get("ETag", "").strip('"')
            with open(local_path, "wb") as f:
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)

            if new_etag:
                with open(etag_path, "w") as f:
                    f.write(new_etag)

            logger.info(f"✅ Updated: {url}")
            return True, new_etag

    except urllib.error.HTTPError as e:
        if e.code == 304:
            return False, last_etag
        raise


def sha256_file(path: str) -> str:
    """Compute file SHA256."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()
