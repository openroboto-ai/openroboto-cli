# ═══════════════════════════════════════════════════════════
# π₀.₅ OpenPi Runner — Universal training container
# ═══════════════════════════════════════════════════════════
# Only installs openpi (numpy<2.0), fully isolated from main process.
# Universal entrypoint: runs default training logic or custom script via env.
#
# Usage — default:
#   docker run --gpus all robot-train-openpi:latest
#
# Usage — custom training strategy:
#   docker run --gpus all \
#     -v /host/strategy.py:/data/scripts/strategy.py \
#     -e CUSTOM_TRAIN=/data/scripts/strategy.py \
#     robot-train-openpi:latest
#
# ═══════════════════════════════════════════════════════════
FROM nvidia/cuda:12.4.1-runtime-ubuntu22.04

WORKDIR /app

# ─── System deps ───────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3.11 python3.11-dev python3.11-venv \
    git curl ca-certificates \
    build-essential pkg-config \
    && rm -rf /var/lib/apt/lists/*

# Install uv directly (no pip needed)
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:$PATH"
ENV UV_SYSTEM_PYTHON=1

RUN uv venv --python python3.11 /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
ENV PYTHONDONTWRITEBYTECODE=1

# ─── Install openpi only (numpy<2.0) ──────────────────
ENV GIT_LFS_SKIP_SMUDGE=1
# 🔴 Same two names as the LingBot context, and for the same reason: `openroboto
#    build` passes them without knowing which base model this season uses, and an
#    unmatched build arg is *silently ignored*.
#
# ⚠️ The default is `main` -- **unpinned**, which is what it has always been. That
#    means two miners building on different days can get different openpi, and
#    the season row has no idea. Pinning it is a decision for whoever sets
#    `params.training.code`; this only makes it possible to.
ARG CODE_REPO=https://github.com/Physical-Intelligence/openpi.git
ARG CODE_REF=main
RUN uv pip install --python /opt/venv/bin/python \
    "git+${CODE_REPO}@${CODE_REF}"

COPY train_runner.py /app/train_runner.py

# Volume mount points (mounted by main process):
# /data/input    — Training data (train.json, val.json)
# /data/output   — Training output (adapter/, metrics.json, proof.json)
# /data/cache    — π₀.₅ checkpoint cache (auto-caches GCS models)
# /data/scripts  — Custom training script (optional, mount with -e CUSTOM_TRAIN=/data/scripts/my_train.py)
RUN mkdir -p /data/input /data/output /data/cache /data/scripts
ENV CUSTOM_TRAIN=""
ENV OPENPI_DATA_HOME=/data/cache
ENV PYTHONUNBUFFERED=1

# Training parameters passed via environment variables (see train_runner.py)
# CHECKPOINT_PATH, TRAIN_DATA, OUTPUT_DIR, EPOCHS, BATCH_SIZE, LR, ...
ENTRYPOINT ["python", "/app/train_runner.py"]
