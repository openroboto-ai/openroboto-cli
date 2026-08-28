"""User-facing CLI for the OpenRoboto subnet (Bittensor netuid 80).

Package `openroboto`, command `openroboto`. Everything a miner or an external
validator can type lives here: from initializing the training environment to
submitting on chain, without cloning any repository at any point.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

__version__: Final = "0.1.0a5"
"""Client version.

Its predecessor `rt.py` had no version number anywhere in the file -- when a
miner reported "my submission failed", there was no way whatsoever to know
which revision of the code they were running; you could only guess.
Every log line written before going on chain prints this number (so does
`openroboto --version`), so reading a log later tells you the client at a
glance.
"""

GITHUB_REPO_URL: Final = "https://github.com/openroboto-ai/openroboto-cli.git"
"""Public repository URL, the single source.

The default in `scripts/deploy_miner.sh` was always the placeholder
`your-org/robot-train-subnet`, and that repository does not exist at all --
following the docs was bound to fail at the git clone step.
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
    script). They used to live in `openpi-runner/` at the repository root and
    *not* ship, with `openroboto build` falling back to docker's remote git
    context. That was broken in two ways:

    1. The repository is private until launch, so the anonymous fetch that
       `docker build <git-url>` performs returned **HTTP 401** -- for every miner
       who installed from PyPI, `openroboto build` could not work at all.
    2. It pinned `#main`, so a miner on a pinned CLI version would build the
       image from whatever `main` happened to be. The container interface
       (mount points, env var names, the `train(cfg, episodes, policy)`
       signature) is red line #2 -- fixed on purpose. Resolving the image
       definition from a moving branch is exactly how the two sides drift apart.

    Shipping it makes both impossible: no network, no credentials, and the image
    definition is versioned with the code that drives it.
    """
    packaged = Path(__file__).parent / "runner"
    return packaged if profile == DEFAULT_RUNNER_PROFILE else packaged / profile
