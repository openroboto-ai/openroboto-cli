# ═══════════════════════════════════════════════════════════
# RobotTrain — π₀.₅ VLA Training Subnet (Public)
# Main process only installs bittensor (numpy>=2.0)
# openpi training runs in an isolated container (openpi-runner/)
# ═══════════════════════════════════════════════════════════
FROM nvidia/cuda:12.4.1-runtime-ubuntu22.04

WORKDIR /app

# ─── System deps ───────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3.11 python3.11-dev python3.11-venv \
    git curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ENV UV_SYSTEM_PYTHON=1
RUN pip install uv

RUN uv venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
ENV UV_PROJECT_ENVIRONMENT="/opt/venv"
ENV PYTHONDONTWRITEBYTECODE=1

# ─── Install bittensor SDK (numpy>=2.0) ────────────────
RUN uv pip install bittensor==10.5.0

# ─── Install extra runtime deps ────────────────────────
COPY requirements.txt .
RUN uv pip install --no-cache-dir -r requirements.txt

# ─── Copy application ──────────────────────────────────
COPY . /app

# ─── Runtime dirs ──────────────────────────────────────
RUN mkdir -p /models /logs

# ─── Entrypoint ────────────────────────────────────────
ENTRYPOINT ["python"]
CMD ["miner.py", "--config", "miner.yaml"]
