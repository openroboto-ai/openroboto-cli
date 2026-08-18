"""命令装配入口。`openroboto` 这个可执行文件就是 `main()`。

这里只做三件事：装配子命令、配日志、把已知异常翻译成一行人话 + 非零退出码。
业务逻辑一律在 `commands/` 下，一个子命令一个模块。
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
    announce,
    build,
    burn,
    check,
    doctor,
    init,
    status,
    submit,
    train,
    upload,
    validator,
)
from openroboto.config import ConfigError, ControlFetchError
from openroboto.console import fail
from openroboto.huggingface import UploadError
from openroboto.logging import setup_logger
from openroboto.payment import BurnError
from openroboto.round_state import StateError
from openroboto.training import TrainingError

COMMAND_MODULES: tuple[ModuleType, ...] = (
    init,
    doctor,
    build,
    train,
    check,
    upload,
    burn,
    announce,
    submit,
    status,
    validator,
)

FILE_LOG_COMMANDS = frozenset({"train", "submit", "burn", "announce", "validator"})
"""这几条要么花钱要么跑几个小时，日志必须落盘 —— 出事时它是唯一的现场。
其余命令只往 stderr 打，不在矿工的目录里凭空造 `logs/`。"""

EXPECTED_ERRORS = (
    BackendError,
    BurnError,
    ChainError,
    ConfigError,
    ControlFetchError,
    StateError,
    TrainingError,
    UploadError,
)
"""这些异常带着给矿工看的说明，直接打消息，不打堆栈。"""


def version_string() -> str:
    """CLI 版本 + 协议包版本。报障时先要这一行。"""
    try:
        protocol_version = version("openroboto-protocol")
    except PackageNotFoundError:  # pragma: no cover —— 正常安装必然有
        protocol_version = "未安装"
    return f"openroboto {__version__} (openroboto-protocol {protocol_version})"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="openroboto",
        description="OpenRoboto 子网（Bittensor netuid 80）矿工与外部验证者 CLI",
    )
    parser.add_argument("--version", action="version", version=version_string())
    parser.add_argument("--log-dir", default="logs", help="日志目录（长命令才写文件）")
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
        fail("已中断")
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
