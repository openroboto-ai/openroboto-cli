"""`openroboto init` —— 释放配置模板与训练策略脚本。

目的很具体：**矿工全程零 clone**。以前要 `git clone` 整个子网仓库才能拿到
`miner.example.yaml` 和一份能改的策略脚本，现在 `pip install openroboto` 之后
一条命令就有了。模板打在 wheel 里（`openroboto/templates/`）。
"""

from __future__ import annotations

import argparse
from importlib.resources import files
from pathlib import Path

from openroboto.console import hint, say

STRATEGIES = ("simple", "example")
"""simple = 能跑通全流程的最小实现；example = 带注释的教学版。"""

CONFIG_TEMPLATE = {"miner": "miner.yaml", "validator": "validator.yaml"}


def add_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "init", help="生成 miner.yaml 与训练策略脚本（零 clone）"
    )
    parser.add_argument(
        "directory", nargs="?", default=".", help="目标目录，默认当前目录"
    )
    parser.add_argument(
        "-s",
        "--strategy",
        choices=STRATEGIES,
        default="simple",
        help="释放哪一份策略脚本（默认 simple）",
    )
    parser.add_argument(
        "--validator",
        action="store_true",
        help="生成 validator.yaml（外部验证者），不生成策略脚本",
    )
    parser.add_argument("--force", action="store_true", help="覆盖已存在的文件")
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

    for path in written:
        say(f"✅ {path}")

    say("")
    if args.validator:
        say(
            "下一步：填 validator.yaml 的 backend.public_key，"
            "然后 `openroboto validator run`"
        )
    else:
        say("下一步：")
        say("  1. 填 miner.yaml 的 huggingface.token / username 与 subnet.hotkey_ss58")
        say("  2. `openroboto doctor` —— 花钱之前把环境问题查掉")
        say("  3. `openroboto build` → `openroboto train` →")
        say("     `openroboto check` → `openroboto submit`")
    return 0


def _template(relative: str) -> str:
    """读打进包里的模板。"""
    return (files("openroboto") / "templates" / relative).read_text(encoding="utf-8")


def _write(path: Path, content: str, force: bool) -> Path:
    """写文件；已存在且没给 --force 就跳过 —— 不能默默盖掉矿工填好的配置。"""
    if path.exists() and not force:
        hint(f"⏭️  已存在，跳过：{path}（要覆盖加 --force）")
        return path
    path.write_text(content, encoding="utf-8")
    return path
