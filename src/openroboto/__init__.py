"""User-facing CLI for the OpenRoboto subnet (Bittensor netuid 80).

Package `openroboto`, command `openroboto`. Everything a miner or an external
validator can type lives here: from initializing the training environment to
submitting on chain, without cloning any repository at any point.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

__version__: Final = "0.1.0a1"
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

OPENPI_RUNNER_CONTEXT: Final = "openpi-runner"
"""Local build-context directory name, checked first by `openroboto build`.

Only a developer override: it lets you build from an edited Dockerfile without
reinstalling. Miners do not have this directory -- the real context ships inside
the package, see `runner_context()`.
"""


def runner_context() -> Path:
    """Directory holding the training image's build context, inside the package.

    This ships in the wheel (~20 KB: a Dockerfile plus one stdlib-only script).
    It used to live in `openpi-runner/` at the repository root and *not* ship,
    with `openroboto build` falling back to docker's remote git context. That was
    broken in two ways:

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
    return Path(__file__).parent / "runner"
