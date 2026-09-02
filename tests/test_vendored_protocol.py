"""The vendored copy under `protocol/` is gone; this file pins "it must not come back".

Background: this protocol code once existed as a separate copy in four repositories,
with no version number and no consistency check. `protocol/types.py` had drifted by
105 lines and `payment.py` by 313 lines -- so miners encoded per copy A while the
backend decoded per copy B. `openroboto-protocol` was extracted precisely to kill
those copies.

**2026-08-19: the three files were deleted along with the old layout** (until then
they had been kept on disk as re-export shims, per the "never delete old files" rule
in `SCOPE.md`). This file therefore changed from "guarding one obsolete copy" to
"confirming the copy has not come back in any form", plus one assertion unrelated to
the copies that must nevertheless always hold: the seed example values printed in the
public docs still reproduce.

Why keep these tests after the deletion: drift is not something discipline avoids.
Adding a new copy makes nothing fail; it only makes some future evaluation
irreproducible -- which is exactly how those 105 lines happened.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import openroboto_protocol.seed as pkg_seed

REPO_ROOT = Path(__file__).resolve().parent.parent


def _git_grep(pattern: str, *pathspec: str) -> list[str]:
    """Search **git-tracked** files only; returns `path:line:content`.

    git grep rather than rglob: `.venv/` contains `openroboto_protocol` itself, so
    walking the filesystem would find the package we are searching for.
    """
    result = subprocess.run(
        ["git", "grep", "-nE", pattern, "--", *pathspec],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    # git grep exits 1 when there is no match; that is a normal result, not an error.
    assert result.returncode in (0, 1), result.stderr
    return [line for line in result.stdout.splitlines() if line]


def test_documented_seed_example_still_reproduces() -> None:
    """The public example values in `docs/SEED_GENERATION.md` must keep computing out.

    This one is unrelated to the vendored copies, and is needed **more** now that they
    are gone: change one character of the seed formula and every historical evaluation
    becomes irreproducible -- and miners use these values to verify we are not picking
    seeds targeted at anyone. The protocol package has its own golden-vector tests;
    this one pins the number **printed in `docs/SEED_GENERATION.md`**.

    ⚠️ The inputs and the result are hardcoded here rather than parsed out of the
    document, so this test cannot notice the document being edited. It is the
    other way round: the document has to match these three lines, and it says so
    next to its own copy of them. The middle input is the **competition id**, not
    the payload's `r` -- for the first simulation season they happen to be the
    same number, which is why using the wrong one used to pass.
    """
    block_hash = "0x" + "11" * 32
    competition_id = 1
    assert pkg_seed.derive_seed(block_hash, competition_id, "22" * 32) == 3898936287


def test_no_vendored_protocol_copy_exists() -> None:
    """The repository must not contain any Python file under `protocol/`.

    `.github/workflows/protocol-guards.yml` checks this too (at the CI layer, catching
    every language). Keeping both is deliberate: local `pytest` surfaces it right away,
    and CI blocks commits that bypassed the local hooks.
    """
    copies = _git_grep(".", "*protocol/*.py")
    tracked = subprocess.run(
        ["git", "ls-files", "*protocol/*.py"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    ).stdout.split()
    assert tracked == [], (
        f"a vendored protocol copy is back: {tracked}\n"
        f"anything protocol-related must come from the `openroboto-protocol` package "
        f"(AGENTS.md red line #1)"
    )
    assert copies == []


def test_nothing_imports_a_local_protocol_module() -> None:
    """The repository must not contain `import protocol` / `from protocol import ...`.

    The copies are gone, but the import statements can still be written (for example
    cherry-picked back from an old branch). That would raise ImportError rather than
    silently resolving to a copy -- but it must still be blocked, because the next step
    is someone "restoring the missing files".
    """
    hits = _git_grep(r"^[[:space:]]*(from|import) protocol([. ]|$)", "*.py")
    assert hits == [], f"something still imports a local protocol module: {hits}"


def test_docs_do_not_teach_miners_to_import_the_copy() -> None:
    """Miner-facing docs must always point at `openroboto_protocol`.

    A line like `from protocol.seed import derive_seed` in the docs is more dangerous
    than in the code: the people copying it are not on the team, and when they cannot
    reproduce a seed they will not ask us -- they will decide the backend got it wrong.
    """
    assert _git_grep(r"^[[:space:]]*(from|import) protocol([. ]|$)", "*.md") == []
