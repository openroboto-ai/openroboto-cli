"""Command assembly entry point. The `openroboto` executable is `main()`.

It does exactly three things: assemble the subcommands, configure logging, and
translate known exceptions into one line of plain language plus a non-zero exit
code. Business logic always lives under `commands/`, one module per subcommand.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from importlib.metadata import PackageNotFoundError, version
from types import ModuleType

from openroboto import __version__
from openroboto.backend_api import BackendError
from openroboto.chain.connection import ChainError
from openroboto.commands import (
    build,
    check,
    doctor,
    init,
    status,
    submit,
    train,
    validator,
)
from openroboto.config import ConfigError, ControlFetchError
from openroboto.console import fail
from openroboto.huggingface import UploadError
from openroboto.logging import setup_logger
from openroboto.payment import BurnError, TransferError
from openroboto.round_state import StateError
from openroboto.training import TrainingError

#: The whole command surface, in the order a miner uses it.
#:
#: `upload` / `burn` / `announce` were removed for 1.0: each was one step of
#: `submit`, and running a step alone is how a fee gets paid for a submission
#: that is never announced, or a commitment announced without a fee. Their
#: implementations stay — `submit` calls them in order.
COMMAND_MODULES: tuple[ModuleType, ...] = (
    init,
    doctor,
    build,
    train,
    check,
    submit,
    status,
    validator,
)

FILE_LOG_COMMANDS = frozenset({"train", "submit", "validator"})
"""These either spend money or run for hours, so their logs must hit disk --
when something goes wrong it is the only crime scene. Every other command only
prints to stderr and does not conjure a `logs/` into the miner's directory."""

EXPECTED_ERRORS = (
    BackendError,
    BurnError,
    ChainError,
    ConfigError,
    ControlFetchError,
    StateError,
    TrainingError,
    TransferError,
    UploadError,
)
"""These exceptions already carry an explanation written for miners: print the
message directly, not a stack trace."""


def version_string() -> str:
    """CLI version + protocol package version. The first line we ask for when
    a problem is reported."""
    try:
        protocol_version = version("openroboto-protocol")
    except PackageNotFoundError:  # pragma: no cover -- a normal install has it
        protocol_version = "not installed"
    return f"openroboto {__version__} (openroboto-protocol {protocol_version})"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="openroboto",
        description=(
            "CLI for miners and external validators of the OpenRoboto subnet "
            "(Bittensor netuid 80)"
        ),
    )
    parser.add_argument("--version", action="version", version=version_string())
    parser.add_argument(
        "--log-dir",
        default="logs",
        help="Log directory (only long-running commands write files)",
    )
    parser.add_argument("--log-level", default="INFO", help="DEBUG | INFO | WARNING")

    subparsers = parser.add_subparsers(dest="command")
    for module in COMMAND_MODULES:
        module.add_parser(subparsers)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return 1

    _configure_logging(args.command, args.log_dir, args.log_level)

    try:
        return int(args.handler(args))
    except EXPECTED_ERRORS as exc:
        fail(str(exc))
        return 1
    except KeyboardInterrupt:
        fail("Interrupted")
        return 130


def _configure_logging(command: str, log_dir: str, log_level: str) -> None:
    if command in FILE_LOG_COMMANDS:
        setup_logger("openroboto", log_dir=log_dir, level=log_level)
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    package_logger = logging.getLogger("openroboto")
    package_logger.setLevel(getattr(logging, log_level.upper(), logging.INFO))
    if not package_logger.handlers:
        package_logger.addHandler(handler)


if __name__ == "__main__":
    sys.exit(main())
