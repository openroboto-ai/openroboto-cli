"""打包不变量。

两条都在 AGENTS.md 的红线里，坏了不会有人立刻发现 —— 只会看到矿工装完用不了。
"""

from __future__ import annotations

import subprocess
from importlib.resources import files
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent.parent / "src" / "openroboto"
REPO_ROOT = Path(__file__).resolve().parent.parent


def test_package_does_not_vendor_the_protocol() -> None:
    """协议实现一律从 `openroboto-protocol` 装，本包不许有自己的一份。

    历史上这份代码在四个仓库里各存一份，已经漂了（`protocol/types.py` 差 105 行，
    `payment.py` 差 313 行）—— 于是矿工按 A 编码、后端按 B 解码。
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
    assert vendored.stdout.strip() == "", f"疑似复制了协议实现：{vendored.stdout}"


def test_templates_are_installed_with_the_package() -> None:
    """模板必须能从**安装好的包**里读出来，否则 `openroboto init` 是空的。

    `.gitignore` 里的 `*.yaml` 曾经把这两份模板一起吞掉；从干净 clone 构建出的
    wheel 里没有它们，而本地开发时看不出来。
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
        "模板被 .gitignore 吞了，clone 出来的仓建不出可用的包"
    )
