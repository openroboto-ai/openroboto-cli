# ═══════════════════════════════════════════════════════════
# LingBot-VLA 2.0 Runner — training container
# ═══════════════════════════════════════════════════════════
# Same shape as the openpi runner one level up (CUDA 12.4 runtime + uv +
# a python3.11 venv), same container interface (red line #2): the mount
# points, the environment variable names and the
# `train(cfg, episodes, policy)` signature are identical. Only what is
# installed, and what `policy` is built out of, differ.
#
# Usage — custom training strategy (the only supported flow, see
# train_runner.py):
#   docker run --gpus all \
#     -v /host/strategy.py:/data/scripts/strategy.py \
#     -e CUSTOM_TRAIN=/data/scripts/strategy.py \
#     lingbot-runner:latest
#
# ✅ **VERIFIED ON A GPU, 2026-08-26.** `docker build` and
# `scripts/verify_lingbot_runner.py` both ran green on an A100-SXM4-80GB
# (driver 580.126.09, Docker 27.3.1); the resulting image is 10.9 GB. That run
# is what moved `adapters.sim_lingbot.training` to `DOCKER`.
#
# ⚠️ The ⚠️ marks below are kept, not cleared. They flag pins and versions
# that were *observed to work together once*, on one driver, against one
# checkpoint revision — re-run the script after touching any of them. What no
# run here has covered is a training step: the verification builds the model,
# injects LoRA, merges and exports, and stops.
# ═══════════════════════════════════════════════════════════
FROM nvidia/cuda:12.4.1-runtime-ubuntu22.04

WORKDIR /app

# ─── System deps ───────────────────────────────────────
# `git` is not optional here (it is in the openpi image): the LingBot training
# code is not on PyPI, it is a repository that gets cloned below.
RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl ca-certificates \
    build-essential pkg-config \
    && rm -rf /var/lib/apt/lists/*

RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:$PATH"

# ─── The interpreter, downloaded rather than apt-installed ──
# 🔴 `apt-get install python3.11` on ubuntu 22.04 gives **3.11.0~rc1**, a
# release candidate — that is the only python3.11 in that distribution's
# archive, and `python -VV` inside the image said
# `Python 3.11.0rc1 (main, Jun 29 2026) [GCC 11.4.0]`.
#
# It is not a cosmetic version difference. manylinux `cp311` wheels are built
# against the *final* 3.11 C ABI, and Triton's compiler frontend is such an
# extension. Measured on an A100-SXM4-80GB, 2026-08-26, on the rc1 image: any
# kernel whose body contains a nested `@triton.jit` call, a `tl.cumsum`, or a
# `tl.histogram` over a full block **segfaults the process** inside
# `ast_to_ttir` — while a plain load/multiply/store kernel compiles and runs.
# Same crash on triton 3.3.1, 3.4.0 and 3.5.0, and an unlimited stack rlimit
# does not change it, so it is the interpreter and not the pin.
#
# That is not a corner: LingBot's fused MoE is built out of exactly those
# kernels (`lingbotvla/ops/group_gemm/`), so on the rc1 image the **first
# training step dies with SIGSEGV and no traceback**, hours after the weights
# were loaded.
#
# ⚠️ Do not put the apt packages back "to have a system python". A
# release-candidate interpreter on `PATH` is the trap; there is nothing in this
# image that needs one.
RUN uv python install 3.11
RUN uv venv --python-preference only-managed --python 3.11 /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
ENV PYTHONDONTWRITEBYTECODE=1

# ─── Torch, pinned to LingBot's requirements.txt ───────
# ⚠️ Unverified: torch 2.8.0's PyPI wheel is built against **CUDA 12.8** while
# this base image is 12.4 runtime. That is supposed to be fine -- the wheel
# bundles its own CUDA runtime and only the *driver* has to be new enough --
# but it depends on the miner's driver (r525+ with forward compatibility), and
# nobody has run it. If it turns out not to be, the fix is one of: move the
# base to `nvidia/cuda:12.8.*-runtime`, or install torch from the cu124 index.
#
# torchvision comes along because the Qwen-VL image path uses it. torchaudio /
# torchcodec / torchdata / av / imageio / opencv are **left out on purpose**:
# they belong to LingBot's LeRobot dataset pipeline (parquet + mp4 decoding),
# and this container never enters it -- `episodes` arrives from the CLI as
# already-decoded JSON, over the mount red line #2 fixes.
RUN uv pip install --python /opt/venv/bin/python \
    torch==2.8.0 torchvision==0.23.0 triton==3.4.0

# ─── flash-attn: a prebuilt wheel, not a source build ──
# LingBot's model code asks for `flash_attention_2` (`build_foundation_model`
# defaults `attn_implementation` to it), and `requirements.txt` does not list
# flash-attn at all -- the official instructions are
# `pip install --no-build-isolation flash-attn==2.8.3`, which on PyPI is an
# **sdist only**: it compiles, and this base image is the `runtime` flavour
# with no nvcc. Rather than swap in the several-GB `devel` base for one
# package, take Dao-AILab's prebuilt wheel.
#
# ⚠️ **This URL pins four things on one line**: cu12 × torch2.8 × cxx11abi ×
# cp311. Bump torch or the python version and this 404s at build time -- which
# is the good outcome; the bad one is a hand-edited URL that installs and then
# mismatches the ABI at import. The wheel's existence was checked against the
# GitHub release API on 2026-08-26; that it *runs* has not been.
# `scripts/verify_lingbot_runner.py` imports it and reports the version.
ARG FLASH_ATTN_WHEEL=https://github.com/Dao-AILab/flash-attention/releases/download/v2.8.3/flash_attn-2.8.3+cu12torch2.8cxx11abiTRUE-cp311-cp311-linux_x86_64.whl
RUN uv pip install --python /opt/venv/bin/python --no-deps "${FLASH_ATTN_WHEEL}"

# ─── The rest of the training deps ─────────────────────
# Pins are LingBot's own (`requirements.txt` @ the commit below). This is a
# **subset**: that file installs everything the whole repository can do --
# dataset conversion, evaluation, serving, plotting -- and the trainer needs a
# fraction of it. Every line here has a caller on the path this container
# actually walks (build the model → hand it to the strategy script):
#
#   transformers/tokenizers  `build_foundation_model` loads through them, and
#                            the model classes subclass `PreTrainedModel`
#   safetensors/huggingface-hub  the 6.38 B checkpoint is safetensors shards
#                            pulled by `snapshot_download`
#   numpy==1.26.4            pinned <2.0 by LingBot; it is also what keeps this
#                            container off the host interpreter (red line #2)
#   peft==0.15.2             `add_lora_to_model` builds a `LoraConfig` and calls
#                            `inject_adapter_in_model` -- this is the whole
#                            reason a 24 GB card can post-train a 6.38 B model
#   einops                   used inside the model's attention/expert blocks
#   pillow / qwen-vl-utils   turning an episode's images into model inputs
#   pyyaml                   LingBot's own config loading
#   tqdm                     imported by `lingbotvla.utils.helper`
#
# Deliberately **not** installed, so that the reason is on record rather than
# rediscovered: datasets / pandas / pyarrow / h5py / jsonlines / zstandard /
# fsspec / aiohttp / blobfile / scipy (the dataset pipeline -- see the torch
# note above); wandb / tensorboard / matplotlib / seaborn / rich / ipdb
# (experiment logging and debugging, which write to services a miner does not
# have); omegaconf / msgpack / websockets / psutil / packaging / ninja /
# sentencepiece / protobuf (used by paths this container does not enter, and
# protobuf==3.20.3 in particular is an old pin that fights other packages).
# `lerobot` is not installed either -- and note it is not in LingBot's
# requirements.txt to begin with.
RUN uv pip install --python /opt/venv/bin/python \
    transformers==4.57.3 tokenizers==0.22.2 \
    safetensors==0.5.3 huggingface-hub==0.34.3 \
    numpy==1.26.4 peft==0.15.2 einops==0.8.1 \
    pillow==11.3.0 qwen-vl-utils==0.0.11 \
    pyyaml==6.0.2 tqdm==4.67.1

# ─── LingBot's training code ───────────────────────────
# Pinned to a commit, never to a branch. `runner_context()` explains why for
# this package's own build context, and the same argument applies twice as hard
# here: the entry point in train_runner.py calls `build_foundation_model()` and
# `add_lora_to_model()` by keyword, and a branch that renames one argument turns
# a pinned CLI release into a container that cannot build a model.
#
# ⚠️ When this moves, re-run `scripts/verify_lingbot_runner.py`: it compares
# the real signatures against what train_runner.py passes, which is the failure
# this pin exists to make loud.
ARG LINGBOT_REF=951475ae1b1d87553e7dc47c97b53a3d695c0d13
ARG LINGBOT_REPO=https://github.com/Robbyant/lingbot-vla-v2.git
RUN git clone --filter=blob:none "${LINGBOT_REPO}" /app/lingbot-vla-v2 \
    && git -C /app/lingbot-vla-v2 checkout --detach "${LINGBOT_REF}"

COPY train_runner.py /app/train_runner.py

# Volume mount points (mounted by the host process -- identical to the openpi
# image, red line #2):
# /data/input    — training data (train.json, val.json)
# /data/output   — training output; this directory **is** the checkpoint root
# /data/cache    — Hugging Face cache (base weights, ~25.5 GB + ~8.9 GB)
# /data/scripts  — custom training script (-e CUSTOM_TRAIN=/data/scripts/my_train.py)
RUN mkdir -p /data/input /data/output /data/cache /data/scripts
ENV CUSTOM_TRAIN=""
ENV PYTHONUNBUFFERED=1

# ⚠️ `build_docker_command()` does not mount /data/cache today (red line #2
# fixes the mount list, and the openpi image never needed it -- it caches under
# CHECKPOINT_PATH). Setting HF_HOME here still pays: without it the ~34 GB of
# base weights land in the container's writable layer and are gone with the
# container. train_runner.py additionally looks for the weights next to
# CHECKPOINT_PATH, which *is* mounted, so a pre-populated checkpoint directory
# avoids the download entirely.
ENV HF_HOME=/data/cache

# LingBot's configs use paths relative to the repository root, and the repo is
# not installed as a package -- so it has to be both the working directory and
# on the import path.
ENV PYTHONPATH=/app/lingbot-vla-v2
WORKDIR /app/lingbot-vla-v2

# Training parameters come in through environment variables (see train_runner.py):
# CHECKPOINT_PATH, TRAIN_DATA, OUTPUT_DIR, EPOCHS, BATCH_SIZE, LR, LORA_R, LORA_ALPHA, HOTKEY
ENTRYPOINT ["python", "/app/train_runner.py"]
