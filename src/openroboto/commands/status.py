"""`openroboto status` —— 查提交状态与被拒原因。

两个端点都**不需要 API key**（2026-08-17 实测）：

- `/api/v1/submissions/history` —— 提交进到队列之后的状态；
- `/api/v1/scan-rejections`     —— 提交在扫链阶段就被拒的原因
  （burn 区块太旧、金额不对、模型撞哈希……）。

「上链了但队列里什么都没有」就是靠第二个端点回答的 —— 这是矿工最常问的问题，
以前只能手 curl。

被拒记录带 `reason` 时会多打两行：稳定错误码，以及**要不要再烧一笔 TAO 重试**。
「基建抖了一下」和「你的模型格式不对」在这里必须一眼可分 —— 猜错的那一边是矿工付账。
"""

from __future__ import annotations

import argparse
from datetime import datetime
from typing import TypeVar

from openroboto_protocol.schemas import Reason, ScanRejection, SubmissionHistoryItem
from openroboto_protocol.status import normalize_status

from openroboto.backend_api import fetch_rejections, fetch_submissions, retry_advice
from openroboto.config import ConfigError, Settings
from openroboto.console import say

DEFAULT_LIMIT = 10

_Row = TypeVar("_Row", SubmissionHistoryItem, ScanRejection)


def add_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("status", help="查提交状态与拒绝原因")
    parser.add_argument("--config", default="miner.yaml")
    parser.add_argument("--hotkey", default="", help="hotkey SS58，默认取 miner.yaml")
    parser.add_argument("--round", type=int, default=0, help="只看某一轮")
    parser.add_argument(
        "--limit", type=int, default=DEFAULT_LIMIT, help="每类最多显示几条"
    )
    parser.set_defaults(handler=run)


def run(args: argparse.Namespace) -> int:
    settings = _load_settings(args.config)
    hotkey = args.hotkey or settings.hotkey_ss58
    if not hotkey:
        raise ConfigError(
            "不知道查谁的提交 —— 用 `--hotkey <SS58>`，"
            "或在 miner.yaml 填 subnet.hotkey_ss58"
        )

    say(f"后端: {settings.backend_url}")
    say(f"hotkey: {hotkey}")
    say("")

    history = fetch_submissions(settings.backend_url, hotkey, args.limit)
    submissions = _by_round(history.data, args.round)
    say(f"提交（{len(submissions)} 条）")
    if not submissions:
        say("  （没有记录。如果你刚 announce，等一个扫链周期再看）")
    for row in submissions:
        say(
            f"  round={row.round_num} "
            f"status={display_status(row)} "
            f"repo={row.hf_repo_id or '?'} "
            f"commit_block={row.commit_block} "
            f"submitted_at={_when(row.submitted_at)}"
        )
    say_more_hint(history.meta.page.has_more, history.meta.page.total)

    rejected = fetch_rejections(settings.backend_url, hotkey, args.limit)
    rejections = _by_round(rejected.data, args.round)
    say("")
    say(f"扫链阶段被拒（{len(rejections)} 条）")
    if not rejections:
        say("  （没有被拒记录）")
    for rejection in rejections:
        say(f"  round={rejection.round_num} burn_block={rejection.burn_block}")
        say(f"    原因: {rejection.reject_reason or '?'}")
        for line in explain(rejection.reason):
            say(f"    {line}")
    say_more_hint(rejected.meta.page.has_more, rejected.meta.page.total)
    if rejections:
        say("")
        say("被拒的 burn 不退款。修掉原因后重新 `openroboto submit`（会烧新的一笔）。")
    return 0


def explain(reason: Reason | None) -> list[str]:
    """把一条 `reason` 摊成矿工能照着做下一步的两行。

    `code` 是稳定机器码（写脚本按它分支），`retryable` 回答「还要不要再烧一笔」。
    老字段 `reject_reason` 照旧打在上面一行 —— 这是加法，不是替换。
    """
    if reason is None:
        return []
    return [
        f"错误码: {reason.code}（来自 {reason.source} 阶段）",
        retry_advice(reason.retryable),
    ]


def say_more_hint(has_more: bool, total: int) -> None:
    """还有没显示出来的记录就说一声。

    `has_more` 由后端算好（`meta.page`），这里不再拿 `offset + len(rows) < total`
    自己推一遍 —— 那个表达式每复制一次就多一次算错的机会，而算错的表现是
    「矿工以为自己只提交了这么几次」。
    """
    if has_more:
        say(f"  （共 {total} 条，这里只显示了前面几条；用 `--limit` 调大）")


def display_status(row: SubmissionHistoryItem) -> str:
    """把后端返回的状态词换成协议词表。

    只读 `eval_status`。**旧的 `status` 列不在这里出现，因为模型里根本没有它** ——
    两列有 52 行不一致，先读 `status` 正是历史上 95 条里 33 条状态显示错的根因。

    TODO(阻断问题 ①)：worker 只认 `done` / `scored` / `failed`，后端给
    `evaluated` / `eval_failed`。这里统一用 protocol 的词表显示；
    等两边定案后，如果需要反向转换（协议词 → worker 词），加在这个函数旁边，
    不要散到各个调用点。
    """
    return normalize_status(row.eval_status) if row.eval_status else "?"


def _when(moment: datetime | None) -> str:
    return moment.isoformat() if moment is not None else "?"


def _by_round(rows: list[_Row], round_num: int) -> list[_Row]:
    if not round_num:
        return rows
    return [row for row in rows if row.round_num == round_num]


def _load_settings(path: str) -> Settings:
    """配置文件读不到也要能查 —— 这条命令是排障用的，不该被配置问题挡住。"""
    try:
        return Settings.load(path)
    except ConfigError:
        return Settings()
