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

## The name and the contents have to come from the same competition

The image **name** comes from the competition (`params.training.image`), the
**contents** come from whatever context is built. The one that ships here
installs openpi and nothing else, so for a competition on another base model
`docker build -t lingbot-runner:1.2 <the openpi context>` produces an image
whose name says one thing and whose contents are another -- and nothing
downstream can tell them apart: `docker images` lists it, `doctor` calls it
ready, `train` runs it, and the miner gets a checkpoint trained on π0.5 under a
LingBot name. There is no error anywhere on that path.

So the pairing is checked instead of assumed: a competition this package has no
container for (`adapters.UNAVAILABLE`) is **refused** rather than built out of
the only context on hand. `--context` remains the way to build an image
definition you brought yourself -- an explicit act, which is the difference
between choosing the contents and defaulting into them.
"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

from openroboto import OPENPI_RUNNER_CONTEXT, adapters, runner_context
from openroboto.config import Settings
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
    parser.add_argument("--config", default="miner.yaml")
    parser.add_argument(
        "--image",
        default="",
        help="image name; defaults to $OPENPI_RUNNER_IMAGE, then to the image "
        "this competition names",
    )
    parser.add_argument(
        "--no-cache", action="store_true", help="build without the layer cache"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the docker command that would run, and stop",
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


def competition_image(config_path: str) -> str:
    """The image this workspace's competition names, or `""`.

    A missing or unreadable config is not an error here: `build` worked without
    one before competitions existed, and the image it built then is still the
    fallback (`runner_image()`).
    """
    if not Path(config_path).is_file():
        return ""
    training = Settings.load(config_path).competition_params.get("training") or {}
    return str(training.get("image") or "") if isinstance(training, dict) else ""


def competition_adapter(config_path: str) -> str:
    """The adapter string this workspace mines, or `""` for a config that has
    none (which `adapters.resolve` reads as the π0.5 simulation competition)."""
    if not Path(config_path).is_file():
        return ""
    return Settings.load(config_path).competition_adapter


def run(args: argparse.Namespace) -> int:
    adapter_name = competition_adapter(args.config)
    if (
        adapters.resolve(adapter_name).training == adapters.UNAVAILABLE
        and not args.context
    ):
        # See the module docstring: building here would name the image after this
        # competition and fill it with the only context that ships, which is
        # π0.5's. Nothing after this point compares the two.
        fail(
            f"This competition (adapter `{adapter_name}`) has no training image "
            f"in this client yet, so there is nothing to build.\n"
            f"   The only image definition that ships here installs openpi "
            f"(π0.5). Building it under this competition's name would leave you "
            f"with an image whose name and contents disagree -- `docker images` "
            f"would list it, `doctor` would call it ready, and training would "
            f"finish on the wrong base model without a single error.\n"
            f"   → have the image definition already? `--context <directory>` "
            f"builds it -- but `openroboto train` still will not drive it until "
            f"this client ships support, so train it your own way and come back "
            f"for `openroboto check` / `openroboto submit`\n"
            f"   → otherwise watch for the announcement, then "
            f"`pip install -U openroboto`"
        )
        return 1

    image = args.image or runner_image(competition_image(args.config))
    context = resolve_context(args.context)
    if not args.context and not Path(OPENPI_RUNNER_CONTEXT).is_dir():
        hint(f"Building from the image definition inside the package ({context})")

    command = build_command(image, context, args.no_cache)
    say(f"🐳 {' '.join(command)}")
    if args.dry_run:
        # Checking that the right image name comes out otherwise means really
        # running `docker build`, which pulls several gigabytes.
        return 0

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
