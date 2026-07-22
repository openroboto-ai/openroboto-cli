#!/usr/bin/env python3
"""
One-time download of π₀.₅ checkpoint to local cache.

Usage:
    python download_checkpoint.py [--force]

Downloads to project root ./cache/pi05_base/ by default
Skips if already present without --force.
"""

import os
import sys
import shutil
import subprocess

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
CHECKPOINT_DIR = os.path.join(PROJECT_ROOT, "cache", "pi05_base")
GCS_URL = "gs://openpi-assets/checkpoints/pi05_base"


def find_gsutil() -> str | None:
    """
    查找 gsutil 可执行文件。
    Find the gsutil executable.
    """
    paths = [
        shutil.which("gsutil"),
        os.path.expanduser("~/google-cloud-sdk/bin/gsutil"),
        "/usr/lib/google-cloud-sdk/bin/gsutil",
        "/opt/google-cloud-sdk/bin/gsutil",
    ]
    for p in paths:
        if p and os.path.exists(p):
            return p
    return None


def install_gsutil() -> bool:
    """
    安装 Google Cloud SDK（提供 gsutil）。
    Install Google Cloud SDK (provides gsutil).

    Returns:
        安装成功返回 True，否则返回 False。
    """
    print("📦 Installing google-cloud-sdk ...")
    try:
        # Check package manager
        if shutil.which("apt-get"):
            subprocess.run([
                "sh", "-c",
                "echo 'deb [signed-by=/usr/share/keyrings/cloud.google.gpg] https://packages.cloud.google.com/apt cloud-sdk main' "
                "| tee /etc/apt/sources.list.d/google-cloud-sdk.list && "
                "curl -fsSL https://packages.cloud.google.com/apt/doc/apt-key.gpg "
                "| gpg --dearmor -o /usr/share/keyrings/cloud.google.gpg && "
                "apt-get update && apt-get install -y google-cloud-sdk"
            ], check=True)
        elif shutil.which("pip") or shutil.which("pip3"):
            pip_cmd = shutil.which("pip") or shutil.which("pip3")
            subprocess.run([pip_cmd, "install", "gcsfs"], check=True)
            return False  # gcsfs installed successfully, but not gsutil
        else:
            print("❌ Cannot install gsutil, please install google-cloud-sdk manually")
            return False
        return True
    except Exception as e:
        print(f"⚠️  Installation failed: {e}")
        return False


def download_with_gsutil(target_dir: str) -> bool:
    """
    使用 gsutil 下载检查点。
    Download checkpoint using gsutil.

    Args:
        target_dir: 目标目录。

    Returns:
        下载成功返回 True，否则返回 False。
    """
    gsutil_path = find_gsutil()
    if not gsutil_path:
        if not install_gsutil():
            gsutil_path = find_gsutil()
            if not gsutil_path:
                print("❌ gsutil not available")
                return False

    print(f"📥 Downloading with gsutil {GCS_URL} → {target_dir}")
    try:
        os.makedirs(target_dir, exist_ok=True)
        # Use -m multi-thread + skip existing files
        result = subprocess.run(
            [gsutil_path, "-m", "rsync", "-r", GCS_URL, target_dir],
            timeout=7200,
        )
        if result.returncode == 0:
            print(f"✅ Download complete: {target_dir}")
            return True
        else:
            print(f"❌ gsutil exit code {result.returncode}")
            return False
    except subprocess.TimeoutExpired:
        print("❌ Download timed out (>2 hours)")
        return False
    except Exception as e:
        print(f"❌ Download failed: {e}")
        return False


def main():
    """
    程序入口：下载 π₀.₅ 检查点到本地缓存。
    Entry point: download π₀.₅ checkpoint to local cache.
    """
    force = "--force" in sys.argv

    if os.path.exists(CHECKPOINT_DIR) and not force:
        # Check if it looks complete
        adapter_files = sum(1 for _ in os.walk(CHECKPOINT_DIR))
        if adapter_files > 10:  # at least some files present
            print(f"✅ Checkpoint already exists: {CHECKPOINT_DIR}")
            print(f"   Use --force to re-download")
            return
        else:
            print(f"⚠️  Incomplete checkpoint directory, re-downloading")

    # Clean old directory
    if os.path.exists(CHECKPOINT_DIR):
        shutil.rmtree(CHECKPOINT_DIR)

    if not download_with_gsutil(CHECKPOINT_DIR):
        print("❌ Download failed, check network or install gsutil manually")
        sys.exit(1)

    print(f"\n📋 Usage:")
    print(f"   Set in miner.conf:")
    print(f"   vla_checkpoint_path: {CHECKPOINT_DIR}")


if __name__ == "__main__":
    main()
