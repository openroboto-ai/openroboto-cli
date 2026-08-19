# ═══════════════════════════════════════════════════════════
# OpenRoboto CLI — π₀.₅ VLA training subnet (netuid 80)
#
# This image holds **only the CLI itself** (bittensor needs numpy>=2.0).
# Training runs in a separate image (`openpi-runner/Dockerfile`, where openpi
# needs numpy<2.0) -- one interpreter cannot hold both numpy versions, which is
# red line #2.
# Consequence: inside this container `openroboto train` reaches out to the
# **host's** Docker to start the training container, so the docker socket has to
# be mounted. See docker-compose.yml.
#
# 2026-08-19: this used to install `requirements.txt` with `python miner.py` as
# the entry point -- the old layout's path, which the new CLI does not import a
# single line of. With the old layout deleted, it now installs this package and
# the entry point is `openroboto`.
# ═══════════════════════════════════════════════════════════
FROM nvidia/cuda:12.4.1-runtime-ubuntu22.04

WORKDIR /app

# ─── System deps ───────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3.11 python3.11-dev python3.11-venv \
    git curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# uv is copied as a binary from the official image, **not installed via pip**.
# This used to be `RUN pip install uv`, but there is no pip on PATH in this CUDA
# base image (python3.11 is installed, Ubuntu ships no pip, and there is no
# python -> python3 alias) -- that line exited 127, which means this image had
# never built successfully.
# The version is pinned (red line #6): uv decides how dependencies resolve, so it
# must not float.
COPY --from=ghcr.io/astral-sh/uv:0.10.11 /uv /usr/local/bin/uv

# 3.11 is named explicitly: the base image defaults python3 to 3.10, while both
# the miner side and CI are on 3.11.
RUN uv venv --python 3.11 /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
# `uv pip install` honours VIRTUAL_ENV, not PATH.
# This used to set `UV_SYSTEM_PYTHON=1`, which makes uv ignore the venv and
# install against the system Python (3.10.12 in the base image) -- and this
# package requires >=3.11, so resolution failed outright.
ENV VIRTUAL_ENV="/opt/venv"
ENV UV_PROJECT_ENVIRONMENT="/opt/venv"
ENV PYTHONDONTWRITEBYTECODE=1

# ─── Install the CLI ───────────────────────────────────
# The build context is this repo (`context: .`). The protocol package installs
# from PyPI -- before 2026-08-19 it was a path dependency on
# `../openroboto-protocol`, which forced the context up one directory. That
# disabled this repo's `.dockerignore` and shipped the whole `rebuild/` tree
# (~620 MB, including the backend's .env and a production database dump) to the
# daemon. Publishing the protocol package removed every one of those workarounds.
#
# COPY only what building the wheel needs, so editing source does not invalidate
# the dependency layer.
# LICENSE has to be present: pyproject sets license-files = ["LICENSE"], and
# without it the build backend reports
# `glob 'LICENSE' did not match any files`.
WORKDIR /build
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN uv pip install --no-cache-dir .

# Once installed the source tree is no longer needed; the working directory goes
# back to /app, where the miner's miner.yaml and state are mounted.
WORKDIR /app

# ─── Runtime dirs ──────────────────────────────────────
# /models is the base-checkpoint cache and /logs is where --log-dir writes; both
# are mounted as volumes in compose.
RUN mkdir -p /models /logs

# ─── Entrypoint ────────────────────────────────────────
# No default subcommand: what you can run in this container is doctor / build /
# train / check / submit / status, and `submit` **spends money**. Defaulting to a
# command that spends money is not acceptable, so this is left empty and compose
# or the command line has to name one explicitly.
ENTRYPOINT ["openroboto"]
CMD ["--help"]
