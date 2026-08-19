"""Command wiring. Every subcommand must parse through to its handler."""

from __future__ import annotations

import pytest

from openroboto import __version__
from openroboto.cli import COMMAND_MODULES, build_parser, main, version_string

COMMANDS = (
    "init",
    "doctor",
    "build",
    "train",
    "check",
    "upload",
    "burn",
    "announce",
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
    assert main(["upload", "--config", "/nonexistent/miner.yaml"]) == 1
    captured = capsys.readouterr()
    assert "openroboto init" in captured.err
    assert "Traceback" not in captured.err
