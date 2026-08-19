"""`openroboto init` -- unpack the config template and the training strategy
script.

The goal is very concrete: **zero cloning for miners, start to finish**.
Getting `miner.example.yaml` and an editable strategy script used to require
`git clone`-ing the whole subnet repository; now one command after
`pip install openroboto` produces them. The templates are packed into the
wheel (`openroboto/templates/`).
"""

from __future__ import annotations

import argparse
from importlib.resources import files
from pathlib import Path

from openroboto.console import hint, say

STRATEGIES = ("simple", "example")
"""simple = the minimal implementation that gets through the whole flow;
example = the annotated teaching version."""

CONFIG_TEMPLATE = {"miner": "miner.yaml", "validator": "validator.yaml"}
README_TEMPLATE = {"miner": "README-miner.md", "validator": "README-validator.md"}


def add_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "init", help="Create a ready-to-use workspace (no clone required)"
    )
    parser.add_argument(
        "directory",
        nargs="?",
        default=".",
        help="target directory, defaults to the current one",
    )
    parser.add_argument(
        "-s",
        "--strategy",
        choices=STRATEGIES,
        default="simple",
        help="which strategy script to unpack (default: simple)",
    )
    parser.add_argument(
        "--validator",
        action="store_true",
        help="write validator.yaml (for external validators) instead of a "
        "strategy script",
    )
    parser.add_argument("--force", action="store_true", help="overwrite existing files")
    parser.set_defaults(handler=run)


def run(args: argparse.Namespace) -> int:
    target = Path(args.directory)
    target.mkdir(parents=True, exist_ok=True)

    role = "validator" if args.validator else "miner"
    written: list[Path] = []

    written.append(
        _write(
            target / CONFIG_TEMPLATE[role],
            _template(CONFIG_TEMPLATE[role]),
            args.force,
        )
    )
    if not args.validator:
        written.append(
            _write(
                target / "train_strategy.py",
                _template(f"{args.strategy}/train_strategy.py"),
                args.force,
            )
        )

    written.append(
        _write(target / "README.md", _template(README_TEMPLATE[role]), args.force)
    )
    # Inside the package it is called `gitignore` (no leading dot): dotfiles
    # are easily swallowed by all sorts of default exclusion rules during
    # packaging and distribution, and when they are swallowed **nothing raises
    # an error** -- it shows up as the file simply not being in the miner's
    # workspace, and the next `git add .` commits their wallet password. The
    # dot is added only when it lands on disk.
    written.append(_write(target / ".gitignore", _template("gitignore"), args.force))

    for path in written:
        say(f"✅ {path}")

    say("")
    if args.validator:
        say(
            "Next: fill in backend.public_key in validator.yaml, "
            "then run `openroboto validator run`"
        )
    else:
        say("Next steps (the workspace README.md has the full walkthrough):")
        say(
            "  1. Fill in huggingface.token / username and subnet.hotkey_ss58 "
            "in miner.yaml"
        )
        say("  2. `openroboto doctor` — catch environment problems before you pay")
        say("  3. `openroboto build` → `openroboto train` →")
        say("     `openroboto check` → `openroboto submit`")
        say("")
        say("⚠️  miner.yaml will hold your wallet password and HF token; .gitignore")
        say("    already excludes it. Do not take it off the ignore list.")
    return 0


def _template(relative: str) -> str:
    """Read a template packed inside the package."""
    return (files("openroboto") / "templates" / relative).read_text(encoding="utf-8")


def _write(path: Path, content: str, force: bool) -> Path:
    """Write the file; skip when it already exists and --force was not given
    -- we must not silently overwrite a config the miner has filled in."""
    if path.exists() and not force:
        hint(f"⏭️  Already exists, skipped: {path} (pass --force to overwrite)")
        return path
    path.write_text(content, encoding="utf-8")
    return path
