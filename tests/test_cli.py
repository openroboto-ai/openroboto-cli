"""Command wiring. Every subcommand must parse through to its handler."""

from __future__ import annotations

import pytest

from openroboto import __version__
from openroboto.cli import COMMAND_MODULES, build_parser, main, version_string

#: The whole command surface, in the order a miner uses it.
#:
#: `upload` / `burn` / `announce` were removed in 1.0: each was one step of
#: `submit`, and a step run alone is how a fee gets paid for a submission that
#: is never announced, or a commitment announced without a fee.
COMMANDS = (
    "init",
    "doctor",
    "build",
    "train",
    "check",
    "submit",
    "status",
    "validator",
)


def test_every_documented_command_is_registered() -> None:
    """This list is the command surface in AGENTS.md. Any one missing breaks a miner's
    scripts."""
    parser = build_parser()
    registered = {
        name
        for action in parser._subparsers._group_actions  # type: ignore[union-attr]
        for name in action.choices
    }
    assert registered == set(COMMANDS)
    assert len(COMMAND_MODULES) == len(COMMANDS)


@pytest.mark.parametrize("command", [c for c in COMMANDS if c != "validator"])
def test_each_command_binds_a_handler(command: str) -> None:
    args = build_parser().parse_args([command])
    assert callable(args.handler)


def test_validator_requires_a_subcommand() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["validator"])
    args = build_parser().parse_args(["validator", "run"])
    assert callable(args.handler)


def test_version_reports_cli_and_protocol() -> None:
    """`rt.py` had no version number anywhere -- when a fault was reported there was no
    way to ask which client version was running."""
    text = version_string()
    assert __version__ in text
    assert "openroboto-protocol" in text


def test_no_command_prints_help_and_fails() -> None:
    assert main([]) == 1


def test_unknown_command_exits_with_argparse_code() -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["nope"])
    assert excinfo.value.code == 2


def test_known_errors_become_one_line_messages(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A missing config is the normal state for a miner, and must not throw a stack
    trace at them."""
    assert main(["submit", "--config", "/nonexistent/miner.yaml"]) == 1
    captured = capsys.readouterr()
    assert "openroboto init" in captured.err
    assert "Traceback" not in captured.err


def test_version_string_matches_pyproject() -> None:
    """🔴 The version lives in two files and both are read by something.

    `pyproject.toml` is what pip installs and resolves against;
    `openroboto.__version__` is what the release workflow compares the git tag
    to, and what `--version` prints. Bumping one and not the other is not
    caught by any other test.

    2026-08-21: bumped pyproject to 0.1.0a2, tagged v0.1.0a2, and the release
    failed at the tag-versus-package check with `__version__` still on 0.1.0a1
    -- after the tag was already pushed, which is the one thing about a release
    that cannot be taken back cleanly.
    """
    import tomllib
    from pathlib import Path

    from openroboto import __version__

    pyproject = Path(__file__).parent.parent / "pyproject.toml"
    parsed = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    declared = parsed["project"]["version"]

    assert __version__ == declared, (
        f"openroboto.__version__ is {__version__} but pyproject.toml says "
        f"{declared} -- the release workflow compares the tag against the former"
    )
