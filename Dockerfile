# ═══════════════════════════════════════════════════════════
# OpenRoboto CLI — π₀.₅ VLA training subnet (netuid 80)
#
# 这个镜像里**只有 CLI 本身**（bittensor 需要 numpy>=2.0）。
# 训练跑在另一个镜像里（`openpi-runner/Dockerfile`，openpi 需要 numpy<2.0）——
# 一个解释器装不下两个 numpy，这是红线 #2。
# 也就是说：这个容器里 `openroboto train` 会去调**宿主的** Docker 起训练容器，
# 所以要挂 docker socket，见 docker-compose.yml。
#
# 2026-08-19：此前这里装的是 `requirements.txt`、入口是 `python miner.py`，
# 也就是旧结构那条路径 —— 而新 CLI 一行都不 import 那些文件。旧结构删除后
# 改成装本包，入口是 `openroboto`。
# ═══════════════════════════════════════════════════════════
FROM nvidia/cuda:12.4.1-runtime-ubuntu22.04

WORKDIR /app

# ─── System deps ───────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3.11 python3.11-dev python3.11-venv \
    git curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# uv 从官方镜像里拷二进制，**不走 pip**。
# 原来这里是 `RUN pip install uv`，而这个 CUDA 基础镜像里 PATH 上没有 pip
# （装了 python3.11 但 Ubuntu 不带 pip，也没有 python→python3 的别名）——
# 那一行 exit 127，也就是说这个镜像此前从来没构建成功过。
# 版本钉死（红线 #6）：uv 会决定依赖怎么解析，不能浮动。
COPY --from=ghcr.io/astral-sh/uv:0.10.11 /uv /usr/local/bin/uv

# 显式指定 3.11：基础镜像的默认 python3 是 3.10，而矿工侧和 CI 都是 3.11。
RUN uv venv --python 3.11 /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
# `uv pip install` 认 VIRTUAL_ENV，不认 PATH。
# 原来这里配的是 `UV_SYSTEM_PYTHON=1`，那会让 uv 无视 venv 去装系统 Python
# （基础镜像里是 3.10.12），而本包要求 >=3.11 —— 解析直接失败。
ENV VIRTUAL_ENV="/opt/venv"
ENV UV_PROJECT_ENVIRONMENT="/opt/venv"
ENV PYTHONDONTWRITEBYTECODE=1

# ─── Install the CLI ───────────────────────────────────
# 构建上下文就是本仓（`context: .`）。协议包从 PyPI 装 —— 2026-08-19 之前它是
# `../openroboto-protocol` 的 path 依赖，逼得 context 必须设成上一级目录，
# 于是本仓的 `.dockerignore` 失效、整棵 `rebuild/` 树（~620 MB，含后端 .env
# 和生产库 dump）会被送进 daemon。协议包发布后这些绕法全部删掉了。
#
# 只 COPY 构建 wheel 需要的东西，改源码不会让依赖层失效。
# LICENSE 必须在：pyproject 里 license-files = ["LICENSE"]，缺了它
# build backend 报 `glob 'LICENSE' did not match any files`。
WORKDIR /build
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN uv pip install --no-cache-dir .

# 装完就不需要源码树了；工作目录换回 /app，矿工的 miner.yaml / state 挂在这里。
WORKDIR /app

# ─── Runtime dirs ──────────────────────────────────────
# /models 是基座缓存，/logs 是 --log-dir 的落点；都在 compose 里挂成卷。
RUN mkdir -p /models /logs

# ─── Entrypoint ────────────────────────────────────────
# 不给默认子命令：这个容器里可以敲的是 doctor / build / train / check /
# submit / status，而 `submit` 会**花钱**。默认跑一个花钱的命令是不可接受的，
# 所以留空，由 compose 或命令行显式指定。
ENTRYPOINT ["openroboto"]
CMD ["--help"]
