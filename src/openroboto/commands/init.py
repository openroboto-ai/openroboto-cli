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
    parser = subparsers.add_parser("init", help="生成开箱即用的工作区（零 clone）")
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
            "下一步：填 validator.yaml 的 backend.public_key，"
            "然后 `openroboto validator run`"
        )
    else:
        say("下一步（工作区的 README.md 里有完整说明）：")
        say("  1. 填 miner.yaml 的 huggingface.token / username 与 subnet.hotkey_ss58")
        say("  2. `openroboto doctor` —— 花钱之前把环境问题查掉")
        say("  3. `openroboto build` → `openroboto train` →")
        say("     `openroboto check` → `openroboto submit`")
        say("")
        say("⚠️  miner.yaml 里会有钱包密码和 HF token，已被 .gitignore 挡掉；")
        say("    别把它移出忽略清单。")
    return 0


def _template(relative: str) -> str:
    """Read a template packed inside the package."""
    return (files("openroboto") / "templates" / relative).read_text(encoding="utf-8")


def _write(path: Path, content: str, force: bool) -> Path:
    """Write the file; skip when it already exists and --force was not given
    -- we must not silently overwrite a config the miner has filled in."""
    if path.exists() and not force:
        hint(f"⏭️  已存在，跳过：{path}（要覆盖加 --force）")
        return path
    path.write_text(content, encoding="utf-8")
    return path
