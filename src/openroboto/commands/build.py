"""`openroboto build` —— 构建 openpi-runner 训练镜像。

镜像定义（`openpi-runner/Dockerfile`）**不进 pip 包**：它是几百行的 CUDA 环境，
装 CLI 的人不需要它躺在 site-packages 里。本地没有这个目录时，直接用 docker 的
**git 远程构建上下文**，矿工照样不用 clone。
"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

from openroboto import GITHUB_REPO_URL, OPENPI_RUNNER_CONTEXT
from openroboto.console import fail, hint, say
from openroboto.training.container import runner_image

BUILD_TIMEOUT_SEC = 7200


def add_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("build", help="构建 openpi-runner 训练镜像")
    parser.add_argument(
        "--context",
        default="",
        help=f"构建上下文，默认 ./{OPENPI_RUNNER_CONTEXT}，本地没有则用公开仓库",
    )
    parser.add_argument(
        "--image", default="", help="镜像名，默认取 $OPENPI_RUNNER_IMAGE"
    )
    parser.add_argument("--no-cache", action="store_true", help="不用构建缓存")
    parser.set_defaults(handler=run)


def resolve_context(explicit: str = "", branch: str = "main") -> str:
    """定位构建上下文：显式 > 本地目录 > 公开仓库的 git 上下文。"""
    if explicit:
        return explicit
    local = Path(OPENPI_RUNNER_CONTEXT)
    if local.is_dir():
        return str(local)
    return f"{GITHUB_REPO_URL}#{branch}:{OPENPI_RUNNER_CONTEXT}"


def build_command(image: str, context: str, no_cache: bool = False) -> list[str]:
    """拼 `docker build` 命令。"""
    command = ["docker", "build", "-t", image]
    if no_cache:
        command.append("--no-cache")
    command.append(context)
    return command


def run(args: argparse.Namespace) -> int:
    image = args.image or runner_image()
    context = resolve_context(args.context)
    if context.startswith("http"):
        hint(f"本地没有 ./{OPENPI_RUNNER_CONTEXT}/，改用远程构建上下文：{context}")

    command = build_command(image, context, args.no_cache)
    say(f"🐳 {' '.join(command)}")

    try:
        completed = subprocess.run(command, timeout=BUILD_TIMEOUT_SEC, check=False)
    except FileNotFoundError:
        fail("找不到 docker。→ 先装 Docker，再跑 `openroboto doctor` 确认")
        return 1
    except subprocess.TimeoutExpired:
        fail(f"构建超过 {BUILD_TIMEOUT_SEC}s，已中止")
        return 1

    if completed.returncode != 0:
        fail(f"镜像构建失败（docker 退出码 {completed.returncode}）")
        return 1

    say(f"✅ 镜像就绪：{image}")
    return 0
