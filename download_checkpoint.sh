#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════
# π₀.₅ Checkpoint Download Script
# ═══════════════════════════════════════════════════════════
# Caches the GCS checkpoint under public/cache/
# to avoid re-downloading every time the openpi-runner container starts.
#
# Usage:
#   cd public
#   bash download_checkpoint.sh [--force]
#
#   --force  Force re-download (deletes old cache)
#   --help   Show help
# ═══════════════════════════════════════════════════════════

set -euo pipefail

# Script runs from public/ directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CACHE_DIR="${SCRIPT_DIR}/cache/pi05_base"
GCS_URL="gs://openpi-assets/checkpoints/pi05_base"
FORCE=0

# ─── Parse args ─────────────────────────────────────────
for arg in "$@"; do
    case "$arg" in
        --force) FORCE=1 ;;
        --help|-h)
            echo "Usage: $0 [--force]"
            echo "  --force  Force re-download"
            exit 0
            ;;
    esac
done

# ─── Check existing ─────────────────────────────────────
if [[ -d "$CACHE_DIR" ]] && [[ -n "$(ls -A "$CACHE_DIR" 2>/dev/null)" ]]; then
    if [[ $FORCE -eq 1 ]]; then
        echo "⚠️  --force mode, deleting old cache..."
        rm -rf "$CACHE_DIR"
    else
        echo "✅ Local checkpoint already exists: $CACHE_DIR"
        echo "   Use --force to re-download"
        exit 0
    fi
fi

# ─── Ensure gsutil ──────────────────────────────────────
find_gsutil() {
    command -v gsutil 2>/dev/null && return 0
    [[ -x "/usr/lib/google-cloud-sdk/bin/gsutil" ]] && echo "/usr/lib/google-cloud-sdk/bin/gsutil" && return 0
    [[ -x "$HOME/google-cloud-sdk/bin/gsutil" ]] && echo "$HOME/google-cloud-sdk/bin/gsutil" && return 0
    return 1
}

install_gsutil() {
    echo "📦 Installing google-cloud-sdk..."
    if command -v apt-get &>/dev/null; then
        apt-get update -qq
        apt-get install -y -qq gnupg ca-certificates
        echo "deb [signed-by=/usr/share/keyrings/cloud.google.gpg] https://packages.cloud.google.com/apt cloud-sdk main" \
            | tee /etc/apt/sources.list.d/google-cloud-sdk.list
        curl -fsSL "https://packages.cloud.google.com/apt/doc/apt-key.gpg" \
            | gpg --dearmor -o /usr/share/keyrings/cloud.google.gpg
        apt-get update -qq
        apt-get install -y -qq google-cloud-sdk
    else
        echo "❌ Unsupported package manager, please install google-cloud-sdk manually"
        exit 1
    fi
}

GSUTIL_PATH=$(find_gsutil || true)
if [[ -z "$GSUTIL_PATH" ]]; then
    install_gsutil
    GSUTIL_PATH=$(find_gsutil || true)
    if [[ -z "$GSUTIL_PATH" ]]; then
        echo "❌ gsutil installation failed, please install manually"
        exit 1
    fi
fi

echo "📥 Downloading with gsutil: $GCS_URL"
echo "   → $CACHE_DIR"
echo "   Size ~12 GB, please wait..."
echo ""

mkdir -p "$CACHE_DIR"

# gsutil rsync with progress + multi-thread
if $GSUTIL_PATH -m rsync -r "$GCS_URL" "$CACHE_DIR"; then
    echo ""
    echo "✅ Download complete: $CACHE_DIR"
    echo ""
    echo "📋 How to use (pick one):"
    echo ""
    echo "  1. Set in config.yaml:"
    echo "     vla_checkpoint_path: $CACHE_DIR"
    echo ""
    echo "  2. Or set env var before running miner.py:"
    echo "     export VLA_CHECKPOINT_PATH=$CACHE_DIR"
    echo ""
    echo "  Or mount in container (docker run):"
    echo "     -v $CACHE_DIR:/data/checkpoint"
    echo "     -e CHECKPOINT_PATH=/data/checkpoint/pi05_base"
else
    echo "❌ Download failed"
    rm -rf "$CACHE_DIR"
    exit 1
fi
