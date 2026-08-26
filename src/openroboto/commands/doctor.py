"""`openroboto doctor` -- check everything checkable before money is spent.

This command exists for exactly one reason: **the experience of "burning TAO
and only then discovering the environment is wrong" has to disappear.** Every
item states "which item is unsatisfied / what the expected value is / how to
fix it", and an unsatisfied item exits non-zero.

There are two classes:
- required items (config / competition / docker / image) -- an unsatisfied
  one fails outright;
- informational items (GPU, HF token, wallet balance) -- when bittensor is not
  installed or there is no card in the environment, they emit a hint but do
  not fail, because commands like `check` and `status` do not need them in the
  first place.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, requires, version

from openroboto import adapters
from openroboto.commands.build import competition_adapter, competition_image
from openroboto.competition import Fee, load_snapshot
from openroboto.config import ConfigError, Settings, environments
from openroboto.console import say
from openroboto.training.container import runner_image

MIN_PYTHON = (3, 11)

#: The package that owns the commitment encoding. Its version **is** the
#: contract version between this CLI and the backend (see the note on the
#: dependency in `pyproject.toml`).
PROTOCOL_PACKAGE = "openroboto-protocol"


@dataclass(frozen=True)
class CheckResult:
    """The verdict of one check. `fix` is the next step for the miner to type
    verbatim."""

    name: str
    ok: bool
    detail: str
    fix: str = ""
    required: bool = True

    def render(self) -> str:
        mark = "✅" if self.ok else ("❌" if self.required else "⚠️ ")
        return f"{mark} {self.name}: {self.detail}"


def add_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "doctor", help="Check the environment: GPU / Docker / config / balance"
    )
    parser.add_argument("--config", default="miner.yaml")
    parser.set_defaults(handler=run)


def run(args: argparse.Namespace) -> int:
    results = [check_python(), check_protocol()]

    settings: Settings | None
    try:
        settings = Settings.load(args.config)
        results.append(CheckResult("config file", True, f"{args.config} parsed"))
    except ConfigError as exc:
        settings = None
        results.append(
            CheckResult(
                "config file", False, str(exc).splitlines()[0], "openroboto init ."
            )
        )

    if settings is not None:
        results.extend(check_settings(settings))
        results.append(check_competition(settings))
        results.append(check_hf_token(settings))
        results.append(check_wallet(settings))

    results.append(check_docker())
    results.append(check_gpu())
    # The competition decides which image `train` runs, so a config that did not
    # parse means checking the historical default rather than guessing one.
    results.append(check_image(args.config if settings is not None else ""))

    for result in results:
        say(result.render())
        if not result.ok and result.fix:
            say(f"   → {result.fix}")

    failed = [r for r in results if not r.ok and r.required]
    say("")
    if failed:
        say(
            f"❌ {len(failed)} required check(s) must be fixed: "
            f"{', '.join(r.name for r in failed)}"
        )
        return 1
    say("✅ All required checks passed")
    return 0


def check_python() -> CheckResult:
    info = sys.version_info
    version = f"{info.major}.{info.minor}.{info.micro}"
    ok = sys.version_info[:2] >= MIN_PYTHON
    return CheckResult(
        "Python",
        ok,
        version,
        f"install Python >= {MIN_PYTHON[0]}.{MIN_PYTHON[1]}",
        required=True,
    )


def check_protocol() -> CheckResult:
    """Whether the installed protocol package is the one this CLI is pinned to.

    This is a money item, not a tidiness item. `openroboto-protocol` is what
    encodes the on-chain commitment, so a miner who upgrades it on its own
    (`pip install -U openroboto-protocol` succeeds and only *warns* about the
    pin) announces bytes the backend does not expect -- and finds out after
    the TAO is gone. `openroboto --version` already prints the number, but
    printing it is not checking it: nobody knows by heart which version this
    release wanted.

    The expected version is read back out of this package's own metadata
    rather than written down again here. `pyproject.toml` already declares
    `openroboto-protocol==<v>`; a second copy of that number in this file
    would drift silently at the next release, which is precisely the failure
    mode this check exists to catch.
    """
    try:
        installed = version(PROTOCOL_PACKAGE)
    except PackageNotFoundError:
        return CheckResult(
            "protocol package",
            False,
            f"{PROTOCOL_PACKAGE} is not installed",
            "pip install -U openroboto",
        )

    pinned = pinned_protocol_version()
    if pinned is None:
        # Running from a source tree that was never installed: there is no
        # declaration to compare against, and guessing one here would be the
        # very second copy this function avoids. Report what is loaded and
        # move on -- a miner never hits this path, a developer does.
        return CheckResult(
            "protocol package",
            True,
            f"{installed} (no pin found to compare against)",
            required=False,
        )
    if installed != pinned:
        return CheckResult(
            "protocol package",
            False,
            f"{installed} installed, this CLI is pinned to {pinned}",
            f"pip install '{PROTOCOL_PACKAGE}=={pinned}' -- the commitment "
            f"encoding lives in that package, so a mismatch is paid for in TAO",
        )
    return CheckResult("protocol package", True, installed)


def pinned_protocol_version() -> str | None:
    """The exact version this package declares for `openroboto-protocol`.

    Returns None when there is nothing to compare against: either this package
    is not installed as a distribution, or the dependency is not pinned with
    `==`. Both mean "cannot tell", which is not the same as "mismatch".
    """
    try:
        declared = requires("openroboto") or []
    except PackageNotFoundError:
        return None
    for requirement in declared:
        name, pin, rest = requirement.partition("==")
        if pin and _normalized(name) == PROTOCOL_PACKAGE:
            # A requirement string may carry an environment marker
            # (`; python_version < "3.12"`) after the version.
            return rest.split(";")[0].strip()
    return None


def _normalized(name: str) -> str:
    """PEP 503 name comparison -- `openroboto_protocol` and
    `openroboto-protocol` are the same distribution."""
    return name.strip().replace("_", "-").lower()


def check_settings(settings: Settings) -> list[CheckResult]:
    """Required fields. Missing ones do not necessarily blow up now, but they
    certainly will blow up at the step that spends money."""
    # environment comes first: it decides what every later item should be. Say
    # so here when they contradict each other, instead of letting the miner get
    # all the way to `burn` and be stopped by `require_for_chain()`.
    conflicts = environments.check_coherent(
        environment=settings.environment,
        network=settings.network,
        netuid=settings.netuid,
        control_json_url=settings.control_json_url,
        backend_url=settings.backend_url,
        competition_source=settings.competition_source,
    )
    results = [
        CheckResult(
            "environment",
            not conflicts,
            (
                f"{settings.environment}"
                f" (network={settings.network} netuid={settings.netuid or 'not set'})"
                if not conflicts
                else "; ".join(c.replace("\n", " ").strip() for c in conflicts)
            ),
            "environment, netuid, network and the URLs must all describe the same "
            "network — change one and you have to change them all",
        ),
        CheckResult(
            "netuid",
            settings.netuid > 0,
            str(settings.netuid) if settings.netuid else "not set",
            "set miner.yaml → subnet.netuid (mainnet is 80)",
        ),
        CheckResult(
            "hotkey_ss58",
            bool(settings.hotkey_ss58),
            settings.hotkey_ss58 or "not set",
            "set miner.yaml → subnet.hotkey_ss58; the HF repo name is derived "
            "from its last 12 characters",
        ),
        CheckResult(
            "HF account",
            bool(settings.hf_username and settings.hf_token),
            f"username={settings.hf_username or 'not set'} "
            f"token={'set' if settings.hf_token else 'not set'}",
            "set miner.yaml → huggingface.username / token (the token needs "
            "write access)",
        ),
    ]
    return results


def check_competition(settings: Settings) -> CheckResult:
    """Which season this workspace mines, and what entering it costs.

    This replaced a `control.json` check on 2026-08-26. That file answered the
    same three questions -- round, status, rate -- for a subnet that ran one
    season at a time, and every one of its answers is now either wrong or
    narrower than the workspace's own:

    * its `round` is a single subnet-wide counter, and it reads `1` while the
      real track's first season and the simulation track's second are both open;
    * its `status` only ever says `active`, where a season has three
      (`draft` / `active` / `archived`);
    * its `burn_rate_tao` is the subnet-wide rate, and `real/1` charges 2 TAO --
      see `_entry_fee` for the wallet that was ticked green at 0.5 TAO.

    It is also the only check here that needed the network, which made an
    unreachable host look like a broken workspace. The season snapshot is
    written into `miner.yaml` by `init` and is what `submit` confirms against,
    so reading it offline is both truer and cheaper.

    🔴 The file itself is **not** retired: external validators still read
    `public_key` out of it to get a rate-limit token, and that URL must not 404.
    What went away is the miner's reason to fetch it.
    """
    snapshot = load_snapshot(settings)
    if snapshot is None:
        return CheckResult(
            "competition",
            False,
            "this workspace does not say which season it mines",
            "`openroboto init --refresh` writes the competition section",
        )
    try:
        fee = snapshot.fee()
    except ConfigError as exc:
        return CheckResult("competition", False, str(exc), "`openroboto init --refresh`")
    return CheckResult(
        "competition",
        True,
        f"{snapshot.name} · {snapshot.status} · "
        f"{fee.amount_tao} TAO to enter, by {fee.kind}",
    )


def check_docker() -> CheckResult:
    if not shutil.which("docker"):
        return CheckResult(
            "Docker",
            False,
            "docker not found",
            "install Docker: https://get.docker.com",
        )
    version = _run(["docker", "--version"])
    if version is None:
        return CheckResult(
            "Docker",
            False,
            "the docker command failed to run",
            "make sure the Docker daemon is running",
        )
    return CheckResult("Docker", True, version)


def check_gpu() -> CheckResult:
    """GPU and the NVIDIA container runtime. check/status run fine without a
    card, so this does not fail."""
    if not shutil.which("nvidia-smi"):
        return CheckResult(
            "GPU",
            False,
            "nvidia-smi not found",
            "training needs the NVIDIA driver; ignore this if you only submit models",
            required=False,
        )
    names = _run(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"])
    toolkit = shutil.which("nvidia-ctk") is not None
    detail = (names or "?").replace("\n", " / ")
    if not toolkit:
        return CheckResult(
            "GPU",
            False,
            f"{detail} (nvidia-container-toolkit missing)",
            "install nvidia-container-toolkit, otherwise `docker run --gpus all` "
            "cannot reach your GPUs",
            required=False,
        )
    return CheckResult("GPU", True, detail)


def check_image(config_path: str = "") -> CheckResult:
    """Is the image `openroboto train` would run actually here?

    Two things this used to get wrong, both silent:

    1. it looked up `runner_image()` with no competition, i.e. the π0.5 default,
       while `train` runs the image `params.training.image` names. Doctor
       reported on an image nothing was going to use;
    2. for a competition this client has no container for, an image under that
       name can still be sitting in `docker images` -- built by an older release
       out of the openpi context, or by hand. "ready" is the one thing that must
       not be said about it: the name came from the competition and the contents
       came from somewhere else, and this is the last place that can say so.

    An empty `config_path` (no config, or one that failed to parse) checks what
    it always did.
    """
    try:
        adapter = adapters.resolve(competition_adapter(config_path))
    except ConfigError as exc:
        return CheckResult(
            "training image",
            False,
            str(exc).splitlines()[0],
            "pip install -U openroboto",
            required=False,
        )
    image = runner_image(competition_image(config_path))

    if not shutil.which("docker"):
        return CheckResult(
            "training image",
            False,
            "no docker, cannot check",
            "install Docker first",
            required=False,
        )
    found = _run(["docker", "images", "-q", image])

    if adapter.training == adapters.UNAVAILABLE:
        # Not a fixable item -- `openroboto build` refuses this competition on
        # purpose -- so it does not fail the run. It still has to be said.
        detail = (
            f"this client has no training image for this competition; `{image}` "
            f"is on this machine but was not built from anything that ships here"
            if found
            else "this client has no training image for this competition yet"
        )
        return CheckResult(
            "training image",
            False,
            detail,
            "`openroboto train` will not run it; train your own way, then "
            "`openroboto check` and `openroboto submit`",
            required=False,
        )

    if not found:
        return CheckResult(
            "training image", False, f"{image} not found", "openroboto build"
        )
    return CheckResult("training image", True, f"{image} ready")


def check_hf_token(settings: Settings) -> CheckResult:
    """Whether the token is valid. An invalid token makes the upload fail only
    after several GB have gone up."""
    if not settings.hf_token:
        return CheckResult(
            "HF token", False, "not set", "set miner.yaml → huggingface.token"
        )
    try:
        from huggingface_hub import HfApi
    except ImportError:
        return CheckResult(
            "HF token",
            False,
            "huggingface_hub is not installed",
            "pip install openroboto",
            required=False,
        )
    try:
        who = HfApi(token=settings.hf_token).whoami()
    except Exception as exc:
        return CheckResult(
            "HF token",
            False,
            f"validation failed: {exc}",
            "use a token that has write access",
        )
    return CheckResult("HF token", True, f"signed in as {who.get('name', '?')}")


def check_wallet(settings: Settings) -> CheckResult:
    """Whether the wallet can be opened, and whether the coldkey balance covers
    this competition's entry fee (`_entry_fee`, not the subnet-wide rate).

    The `except Exception` here is deliberate: exceptions at the wallet layer
    come from the bittensor SDK (`KeyFileError`, substrate connection
    errors, ...), and their types are outside our control. **doctor crashing
    itself is the worst possible outcome** -- a miner runs the health check
    precisely because the environment has a problem, and a health-check tool
    must not fail to produce a report because the environment has a problem.
    """
    try:
        address = _coldkey_address(settings)
    except ImportError:
        return CheckResult(
            "wallet",
            False,
            "bittensor is not installed",
            "pip install openroboto (required to commit on chain)",
            required=False,
        )
    except Exception as exc:
        return CheckResult(
            "wallet",
            False,
            str(exc).splitlines()[0],
            "run `btcli wallet list` and check the coldkey / hotkey names and the "
            "wallet path",
        )

    if not address:
        return CheckResult(
            "wallet",
            True,
            "loaded (coldkey address unreadable, skipping the balance check)",
            required=False,
        )

    try:
        from openroboto.chain import get_subtensor

        subtensor = get_subtensor(settings.network)
        try:
            balance = float(subtensor.get_balance(address))
        finally:
            subtensor.close()
    except Exception as exc:
        return CheckResult(
            "wallet", True, f"loaded (balance unavailable: {exc})", required=False
        )

    priced = _entry_fee(settings)
    # An unknown fee cannot be compared against the balance (`None` does not
    # compare), and pretending it is covered is worse than saying so -- doctor is
    # the self-check entry point before money is spent, so "cannot be determined"
    # is reported as exactly that.
    if priced is None:
        return CheckResult(
            "wallet balance",
            False,
            f"{balance:.4f} TAO (this workspace's entry fee is unknown, so "
            f"whether that is enough cannot be told)",
            "`openroboto init --refresh` rewrites the competition section, which "
            "is where the fee comes from",
        )

    season, fee = priced
    enough = balance >= fee.amount_tao
    return CheckResult(
        "wallet balance",
        enough,
        f"{balance:.4f} TAO ({season} costs {fee.amount_tao} TAO to enter, "
        f"by {fee.kind})",
        "balance too low, top up before you submit — a payment that fails halfway "
        "still has to be redone",
    )


def _entry_fee(settings: Settings) -> tuple[str, Fee] | None:
    """What entering this workspace's competition costs, and which one that is.

    🔴 **Not `settings.burn_rate_tao`.** That is control.json's subnet-wide rate,
    and the subnet runs several seasons at once: on `real/1` it reads 0.1 while
    that season's own `params.fee` is 2 TAO, so a wallet holding 0.5 TAO was
    ticked green here and ran out at `submit` -- after the upload, and with no
    hint anywhere in the report that the two numbers were about different things.
    The season's `params.fee` is the amount `submit` actually confirms and pays,
    so it is the one to hold a balance against.

    `None` means there is nothing to compare with, and both ways of getting there
    are real: a workspace with no `competition:` section (which cannot pay at all
    -- `submit` refuses it), and one whose section will not parse. Neither is
    guessed past; `fee_of` has no defaults for the same reason.
    """
    snapshot = load_snapshot(settings)
    if snapshot is None:
        return None
    try:
        return snapshot.name, snapshot.fee()
    except ConfigError:
        # The unparseable half. Naming the broken key belongs to the command
        # that acts on it; here the honest report is "unknown", which is what
        # the caller prints.
        return None


def _coldkey_address(settings: Settings) -> str:
    """Open the wallet and read out the coldkey public address.

    `wallet.coldkeypub` is **an attribute access that triggers a file read**:
    when there is no coldkeypub.txt in the wallet directory it raises
    `KeyFileError` rather than returning None -- measured on this machine once
    when doctor crashed because of it.
    """
    from openroboto.chain import open_wallet

    wallet = open_wallet(settings)
    coldkeypub = wallet.coldkeypub
    return str(getattr(coldkeypub, "ss58_address", "") or "")


def _run(command: list[str]) -> str | None:
    """Run one read-only command and take its stdout. Gives None on
    failure."""
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, timeout=15, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()
