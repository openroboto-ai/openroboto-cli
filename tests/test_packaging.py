"""Packaging invariants.

Both are red lines in AGENTS.md. If they break, nobody notices right away -- the only
symptom is miners installing the package and finding it unusable.
"""

from __future__ import annotations

import subprocess
from importlib.metadata import version
from importlib.resources import files
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent.parent / "src" / "openroboto"
REPO_ROOT = Path(__file__).resolve().parent.parent

#: The protocol release this repository resolves to. Pinned in `pyproject.toml`;
#: repeated here on purpose -- see the test below.
PROTOCOL_VERSION = "0.8.0"


def test_package_does_not_vendor_the_protocol() -> None:
    """The protocol implementation must always come from `openroboto-protocol`; this
    package must not carry its own copy.

    Historically this code lived as a separate copy in four repositories and had
    drifted (`protocol/types.py` off by 105 lines, `payment.py` off by 313 lines) --
    so miners encoded per copy A while the backend decoded per copy B.
    """
    assert not (PACKAGE_ROOT / "protocol").exists()

    vendored = subprocess.run(
        [
            "grep",
            "-rl",
            "def derive_seed\\|def check_checkpoint_layout",
            str(PACKAGE_ROOT),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert vendored.stdout.strip() == "", (
        f"looks like a copied protocol implementation: {vendored.stdout}"
    )


def test_the_installed_protocol_is_not_the_one_the_compat_baseline_was_taken_on() -> (
    None
):
    """Half of "0.6.0 and 0.7.0 encode the same commitment bytes, to the byte".

    The other half lives in the backward-compatibility task: its baseline was
    recorded on 0.6.0 (`tests/fixtures/baseline/PROTOCOL_VERSION`) and every run
    compares it against what this environment encodes today. If this environment
    were 0.6.0 as well, that comparison would be 0.6.0 against itself -- green,
    and proving nothing about the upgrade a miner is being asked to make.

    So the resolved version is written down here rather than left to whatever
    happened to be installed. Yes, that is a second copy of the number in
    `pyproject.toml`; the failure it exists to catch is precisely the two of them
    disagreeing, which no amount of reading one of them out of metadata can see.

    When the pin moves again: change the number here, and check that the
    baseline was **not** regenerated along with it. Regenerating it is what
    turns the comparison back into a tautology, silently.
    """
    assert version("openroboto-protocol") == PROTOCOL_VERSION


def test_templates_are_installed_with_the_package() -> None:
    """Templates must be readable from the **installed package**, otherwise
    `openroboto init` produces nothing.

    The `*.yaml` entry in `.gitignore` once swallowed both templates: a wheel built
    from a clean clone did not contain them, and local development did not show it.
    """
    templates = files("openroboto") / "templates"
    miner_template = (templates / "miner.yaml").read_text(encoding="utf-8")
    assert miner_template.startswith("# OpenRoboto")
    assert (templates / "validator.yaml").is_file()
    assert (templates / "simple" / "train_strategy.py").is_file()
    assert (templates / "example" / "train_strategy.py").is_file()


def test_templates_are_not_ignored_by_git() -> None:
    ignored = subprocess.run(
        ["git", "check-ignore", "src/openroboto/templates/miner.yaml"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert ignored.returncode != 0, (
        "the templates are swallowed by .gitignore; a fresh clone cannot build a "
        "usable package"
    )
