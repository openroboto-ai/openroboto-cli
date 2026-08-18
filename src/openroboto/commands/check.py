"""`openroboto check` —— 付费前在本地判定 checkpoint 能不能被评测。

以前这一步要 clone 第二个仓库（`openroboto-evaluation` 的
`libero_eval/check_model.py`），实际结果是没人跑 —— 于是「烧完 TAO 才发现传的是
裸 LoRA adapter」成了最常见的白烧方式。

判定规则**不在这里实现**：调 `openroboto_protocol.model_format`，
与后端准入用的是同一套代码、同一批错误码。纯本地、零 GPU、零网络。
"""

from __future__ import annotations

import argparse
from pathlib import Path

from openroboto_protocol.model_format import (
    CheckpointFile,
    FormatReport,
    check_checkpoint_layout,
)

from openroboto.console import say
from openroboto.round_state import resolve_output_dir, resolve_round


def add_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "check", help="付费前本地验证模型格式（评测器用的同一套规则）"
    )
    parser.add_argument(
        "path", nargs="?", default="", help="checkpoint 目录，默认取本轮训练输出目录"
    )
    parser.add_argument("--round", type=int, default=0, help="轮次号，默认自动判断")
    parser.set_defaults(handler=run)


def run(args: argparse.Namespace) -> int:
    directory = Path(args.path or resolve_output_dir(resolve_round(args.round)))
    if not directory.is_dir():
        say(f"❌ 目录不存在：{directory}")
        return 1

    report = check_directory(directory)
    return report_result(directory, report)


def collect_files(directory: Path) -> list[CheckpointFile]:
    """把目录里的文件列成协议包要的清单（相对 POSIX 路径 + 字节数）。"""
    return [
        CheckpointFile(
            path=path.relative_to(directory).as_posix(), size_bytes=path.stat().st_size
        )
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    ]


def check_directory(directory: Path) -> FormatReport:
    return check_checkpoint_layout(collect_files(directory))


def report_result(directory: Path, report: FormatReport) -> int:
    """打印判定结果。返回退出码：0 = 可以提交。"""
    say(f"checkpoint: {directory}")
    say(f"权重形态: {report.kind.value if report.kind else '未识别'}")
    say(f"计入体积: {report.counted_size_bytes / 1024 / 1024:.1f} MB")

    for warning in report.warnings:
        say(f"⚠️  [{warning.code.value}] {warning.message}")

    if report.ok:
        say("✅ 格式通过，可以 `openroboto submit`")
        return 0

    for error in report.errors:
        say(f"❌ [{error.code.value}] {error.message}")
    say("")
    say("→ 现在不要 burn。修完再跑一次 `openroboto check`；被拒的 burn 不退款。")
    return 1
