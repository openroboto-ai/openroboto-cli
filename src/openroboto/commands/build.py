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
    parser = subparsers.add_parser(
        "build", help="Build the openpi-runner training image"
    )
    parser.add_argument(
        "--context",
        default="",
        help="build context; defaults to the copy inside the package, but a local "
        f"./{OPENPI_RUNNER_CONTEXT}/ takes precedence",
    )
    parser.add_argument(
        "--image", default="", help="image name, defaults to $OPENPI_RUNNER_IMAGE"
    )
    parser.add_argument(
        "--no-cache", action="store_true", help="build without the layer cache"
    )
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
        hint(f"Building from the image definition inside the package ({context})")

    command = build_command(image, context, args.no_cache)
    say(f"🐳 {' '.join(command)}")

    try:
        completed = subprocess.run(command, timeout=BUILD_TIMEOUT_SEC, check=False)
    except FileNotFoundError:
        fail(
            "docker not found. → Install Docker, then run `openroboto doctor` "
            "to confirm"
        )
        return 1
    except subprocess.TimeoutExpired:
        fail(f"build ran longer than {BUILD_TIMEOUT_SEC}s and was aborted")
        return 1

    if completed.returncode != 0:
        fail(f"image build failed (docker exit code {completed.returncode})")
        return 1

    say(f"✅ Image ready: {image}")
    return 0
