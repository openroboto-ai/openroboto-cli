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
**contents** come from whatever context is built, and nothing downstream
compares them: `docker build -t lingbot-runner:1.2 <the openpi context>`
produces an image whose name says one thing and whose contents are another --
`docker images` lists it, `doctor` calls it ready, `train` runs it, and the
miner gets a checkpoint trained on π0.5 under a LingBot name. There is no error
anywhere on that path.

So the context is picked by the competition's **format profile** rather than
being whatever ships (`runner_context()`): π0.5 competitions get the openpi
context, LingBot ones get the LingBot context. And the pairing is still checked
rather than assumed -- a competition whose container this package has not
released (`adapters.UNAVAILABLE`) is **refused**, even when a context for its
base model is on hand, because `training` is a claim about `openroboto train`
driving that image and not merely about the image existing.

`--context` remains the way to build an image definition you brought yourself
-- an explicit act, which is the difference between choosing the contents and
defaulting into them.
"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

from openroboto import adapters, local_runner_context, runner_context
from openroboto.config import ConfigError, Settings
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
        help="build context; defaults to the copy inside the package for this "
        "competition's base model, but a local ./<profile>-runner/ "
        "(./openpi-runner/, ./lingbot-runner/) takes precedence",
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


def resolve_context(explicit: str = "", profile: str = adapters.OPENPI) -> str:
    """Resolve the build context: explicit > local `./<profile>-runner/` > the
    one inside the package.

    `profile` is the competition's format profile, which is what decides *which*
    base model the image has to contain. The default is π0.5, the answer for a
    `miner.yaml` with no competition section.

    The last tier **always exists** for a profile this package ships (it goes
    into the wheel), so this function never returns something that cannot be
    reached -- and `run()` refuses before calling it for one that does not.
    """
    if explicit:
        return explicit
    local = local_runner_context(profile)
    if local.is_dir():
        return str(local)
    return str(runner_context(profile))


def build_command(
    image: str, context: str, no_cache: bool = False, code: str = ""
) -> list[str]:
    """Assemble the `docker build` command.

    `code` is `repo@revision` for the model source this season builds against,
    out of `params.training.code`. It reaches the Dockerfile as build args, which
    **already exist** there -- until 2026-08-31 nothing ever passed them, so the
    pinned defaults inside the image were the only answer and moving to a new
    commit took a CLI release.

    ⚠️ Empty means the season names none, and then the Dockerfile's own pins
    apply -- byte-for-byte the behaviour every workspace had before this field.
    """
    command = ["docker", "build", "-t", image]
    if no_cache:
        command.append("--no-cache")
    if code:
        repo, _, revision = code.partition("@")
        # 🔴 `CODE_REPO` / `CODE_REF` are the same two names in **both** runner
        #    contexts, deliberately: this function does not know which base model
        #    the season uses, and a build arg that matches nothing is **silently
        #    ignored by docker build** -- the image would come out on its default
        #    pin with nothing anywhere saying so.
        if repo:
            command += ["--build-arg", f"CODE_REPO={repo}"]
        if revision:
            command += ["--build-arg", f"CODE_REF={revision}"]
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


def competition_code(config_path: str) -> str:
    """`params.training.code` -- `repo@revision` for the model source, or `""`.

    Same shape and same tolerance as `competition_image()`: a workspace without a
    config is not an error here, it just names nothing and the Dockerfile's own
    pins apply.
    """
    if not Path(config_path).is_file():
        return ""
    training = Settings.load(config_path).competition_params.get("training") or {}
    return str(training.get("code") or "") if isinstance(training, dict) else ""


def competition_adapter(config_path: str) -> str:
    """The adapter string this workspace mines, or `""` for a config that has
    none (which `adapters.resolve` reads as the π0.5 simulation competition)."""
    if not Path(config_path).is_file():
        return ""
    return Settings.load(config_path).competition_adapter


def competition_base_model_family(config_path: str) -> str:
    """Which base model this workspace's competition runs on, or `""`.

    Same tolerance as `competition_adapter`: no config is not an error here.
    `adapters.format_profile` is what turns `""` into either the π0.5 default or
    a refusal -- that decision does not belong in a config reader.
    """
    if not Path(config_path).is_file():
        return ""
    return Settings.load(config_path).competition_base_model_family


def packaged_context(adapter_name: str, config_path: str) -> Path | None:
    """The build context this package ships for the workspace's base model, or
    `None` when there is nothing honest to point at.

    `None` covers both "the season has not named a base model" (a real-track
    workspace today) and "it named one this client does not ship a context for".
    Both mean the same thing to the caller -- there is no directory to suggest --
    and neither may become a *default* directory: an image built out of the wrong
    base is invisible afterwards, which is the failure `run()` refuses to cause.
    """
    try:
        profile = adapters.format_profile(
            adapter_name, competition_base_model_family(config_path)
        )
    except ConfigError:
        return None
    context = runner_context(profile)
    return context if context.is_dir() else None


def run(args: argparse.Namespace) -> int:
    adapter_name = competition_adapter(args.config)
    adapter = adapters.resolve(adapter_name)
    # 🔴 The "not released yet" refusal comes **first**, and deliberately does not
    # need to know the base model: it is a property of the competition, and its
    # message is the more actionable of the two. Resolving the base model first
    # would replace "watch for the announcement" with "your config does not say
    # which base model", which is true but not what a miner can act on.
    if adapter.training == adapters.UNAVAILABLE and not args.context:
        # See the module docstring: building without a released container means
        # naming the image after this competition and filling it with whatever
        # context is on hand. Nothing after this point compares the two.
        #
        # The refusal survives the LingBot context shipping. `training` says
        # whether `openroboto train` will **drive** this image, and for LingBot
        # that is still unproven -- nobody has run it on a GPU (see
        # `runner/lingbot/train_runner.py`). Letting `build` succeed here would
        # hand back an image that the next command refuses to touch, which reads
        # as a broken client rather than as an unreleased competition.
        packaged = packaged_context(adapter_name, args.config)
        byo = (
            f"   → have a GPU and want to drive it yourself? this client already "
            f"ships an unverified build context for this base model:\n"
            f"     `openroboto build --context {packaged}`\n"
            if packaged is not None
            else "   → have the image definition already? `--context <directory>` "
            "builds it\n"
        )
        fail(
            f"Training support for this competition (adapter `{adapter_name}`) "
            f"has not been released yet, so `openroboto build` will not build it "
            f"under this competition's name.\n"
            f"   An image whose name and contents disagree is invisible: "
            f"`docker images` lists it, `doctor` calls it ready, and training "
            f"finishes on the wrong base model without a single error.\n"
            f"{byo}"
            f"     `openroboto train` still will not drive it -- train your own "
            f"way, then come back for `openroboto check` / `openroboto submit`\n"
            f"   → otherwise watch for the announcement, then "
            f"`pip install -U openroboto`"
        )
        return 1

    # 🔴 Which image to build follows the **base model**, not the adapter. This
    # used to read `adapter.format_profile`, and for `real_xarm6` that column was
    # a guess baked into the table -- the wrong one (xArm 6 comes up on π0.5).
    profile = adapters.format_profile(
        adapter_name, competition_base_model_family(args.config)
    )
    image = args.image or runner_image(competition_image(args.config))
    context = resolve_context(args.context, profile)
    if not args.context and not local_runner_context(profile).is_dir():
        hint(f"Building from the image definition inside the package ({context})")

    command = build_command(
        image, context, args.no_cache, code=competition_code(args.config)
    )
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
