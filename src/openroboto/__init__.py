"""User-facing CLI for the OpenRoboto subnet (Bittensor netuid 80).

Package `openroboto`, command `openroboto`. Everything a miner or an external
validator can type lives here: from initializing the training environment to
submitting on chain, without cloning any repository at any point.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

__version__: Final = "1.3.0"
"""Client version.

Every log line written before going on chain prints this number (so does
`openroboto --version`), so reading a log later tells you the client at a
glance. Without a version number in the payload, "my submission failed" can
only be answered by guessing which revision the miner was running.
"""

GITHUB_REPO_URL: Final = "https://github.com/openroboto-ai/openroboto-cli.git"
"""Public repository URL, the single source.

Scripts and docs clone from here, **not** from a placeholder such as
`your-org/robot-train-subnet`: no such repository exists, so following the
docs fails at the git clone step.
"""

DEFAULT_RUNNER_PROFILE: Final = "openpi"
"""Which build context `runner_context()` hands back when nobody says.

The value is `adapters.OPENPI` -- spelled out rather than imported, because
`adapters` imports from `openroboto.config`, and importing it from the package
root is a cycle. `tests/test_adapters.py` compares the two.

It is also the answer for a `miner.yaml` written before competitions existed:
that workspace is the π0.5 competition (`adapters.DEFAULT_ADAPTER`), and the
context it built then is the one it must keep building.
"""


def local_runner_context(profile: str = DEFAULT_RUNNER_PROFILE) -> Path:
    """Local build-context directory, checked first by `openroboto build`:
    `./openpi-runner/`, `./lingbot-runner/`.

    Only a developer override: it lets you build from an edited Dockerfile
    without reinstalling. Miners do not have this directory -- the real context
    ships inside the package, see `runner_context()`.
    """
    return Path(f"{profile}-runner")


def runner_context(profile: str = DEFAULT_RUNNER_PROFILE) -> Path:
    """Directory holding this competition's training image build context, inside
    the package.

    There is one context per **format profile**, not one per competition: what
    the image has to contain is decided by the base model (`adapters.OPENPI` /
    `adapters.LINGBOT`), and two competitions on the same base model want the
    same image with different names. Names come from `params.training.image`;
    contents come from here. `commands/build.py` is where the two are kept from
    disagreeing.

    Layout, and why it is lopsided:

        runner/            <- openpi (π0.5), the default profile
        runner/lingbot/    <- LingBot-VLA 2.0

    π0.5's context stays exactly where it was rather than moving down to
    `runner/openpi/`. It is in production, `docker build` on it is the path
    every miner runs today, and a path change buys nothing here. The cost is
    that `lingbot/` sits inside π0.5's build context and gets sent to the
    daemon on every openpi build -- about 20 KB, and the openpi Dockerfile
    COPYs one file by name, so nothing of LingBot's can leak into that image.

    Each context ships in the wheel (~20 KB: a Dockerfile plus one stdlib-only
    script) -- **not** resolved from docker's remote git context at build time.
    A remote context is broken in two ways:

    1. `docker build <git-url>` fetches anonymously, so a private repository
       answers **HTTP 401** and `openroboto build` cannot work at all for a
       miner who installed from PyPI.
    2. A git context pins a branch, so a miner on a pinned CLI version builds
       the image from whatever that branch holds. The container interface
       (mount points, env var names, the `train(cfg, episodes, policy)`
       signature) is red line #2 -- fixed on purpose. Resolving the image
       definition from a moving branch is exactly how the two sides drift apart.

    Shipping it makes both impossible: no network, no credentials, and the image
    definition is versioned with the code that drives it.
    """
    packaged = Path(__file__).parent / "runner"
    return packaged if profile == DEFAULT_RUNNER_PROFILE else packaged / profile
