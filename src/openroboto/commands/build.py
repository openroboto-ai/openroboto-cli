"""`openroboto build` -- build the openpi-runner training image.

The image definition **ships with the package** (`openroboto/runner/`, about
20 KB: one Dockerfile plus one stdlib-only script). Miners do not have to
clone, do not have to be online, and do not need the repository to be public.

It used to live in `openpi-runner/` at the repository root and not ship in the
package, falling back to docker's **remote git build context** when it was
absent locally -- that path was broken at both ends: the repository is private
until launch, so the anonymous fetch that `docker build <git-url>` performs
returns **401**, meaning `build` could not run at all for any miner who
installed via pip; and it pinned `#main`, so a miner on a fixed CLI version
would build from the image definition on `main` -- the container interface
(mount points, environment variable names, the `train()` signature) is red
line #2 and is fixed on purpose, and resolving the image definition from a
moving branch is exactly how the two sides drift apart.

`--context` and a local `./openpi-runner/` still win, which is there for
people editing the Dockerfile.
"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

from openroboto import OPENPI_RUNNER_CONTEXT, runner_context
from openroboto.console import fail, hint, say
from openroboto.training.container import runner_image

BUILD_TIMEOUT_SEC = 7200


def add_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("build", help="构建 openpi-runner 训练镜像")
    parser.add_argument(
        "--context",
        default="",
        help=f"构建上下文。默认用包内那份；本地有 ./{OPENPI_RUNNER_CONTEXT}/ 时优先它",
    )
    parser.add_argument(
        "--image", default="", help="镜像名，默认取 $OPENPI_RUNNER_IMAGE"
    )
    parser.add_argument("--no-cache", action="store_true", help="不用构建缓存")
    parser.set_defaults(handler=run)


def resolve_context(explicit: str = "") -> str:
    """Resolve the build context: explicit > local `./openpi-runner/` > the
    one inside the package.

    The last tier **always exists** (it ships in the wheel), so this function
    never returns something that cannot be reached.
    """
    if explicit:
        return explicit
    local = Path(OPENPI_RUNNER_CONTEXT)
    if local.is_dir():
        return str(local)
    return str(runner_context())


def build_command(image: str, context: str, no_cache: bool = False) -> list[str]:
    """Assemble the `docker build` command."""
    command = ["docker", "build", "-t", image]
    if no_cache:
        command.append("--no-cache")
    command.append(context)
    return command


def run(args: argparse.Namespace) -> int:
    image = args.image or runner_image()
    context = resolve_context(args.context)
    if not args.context and not Path(OPENPI_RUNNER_CONTEXT).is_dir():
        hint(f"用包内的镜像定义构建（{context}）")

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
