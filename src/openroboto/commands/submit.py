"""`openroboto submit` —— 上传 → 烧 → 公告，一条龙（旧 `rt.py submit`）。

三步复用 `upload` / `burn` / `announce` 三个模块里的同一份实现。
旧 `rt.py` 把三步在 `cmd_submit` 里**又抄了一遍**，结果两处的自检和跳过条件
慢慢长歪；这里只有一份。

断点让这条命令天然可重入：上传过就不重传，烧过就不重烧。
"""

from __future__ import annotations

import argparse

from openroboto.commands.announce import perform_announce
from openroboto.commands.burn import perform_burn
from openroboto.commands.upload import perform_upload
from openroboto.config import Settings
from openroboto.console import say
from openroboto.round_state import (
    load_state,
    resolve_output_dir,
    resolve_round,
    save_state,
)


def add_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("submit", help="上传 → 烧 → 公告")
    parser.add_argument("--config", default="miner.yaml")
    parser.add_argument("--round", type=int, default=0)
    parser.add_argument("--output-dir", default="")
    parser.add_argument(
        "--force", action="store_true", help="忽略已完成状态，重新烧一次并重发公告"
    )
    parser.set_defaults(handler=run)


def run(args: argparse.Namespace) -> int:
    settings = Settings.load(args.config)
    round_num = resolve_round(args.round)
    output_dir = args.output_dir or resolve_output_dir(round_num)
    state = load_state(round_num)

    say(f"🦞 submit | round={round_num}")

    if state.get("step") == "announce" and not args.force:
        say("⏭️  这一轮已经提交完成。要重来加 --force（会再烧一次 TAO）")
        return 0

    if args.force:
        say("⚡ --force：忽略断点里的 burn，会**再烧一笔**")
        state.pop("burn_tx_hash", None)
        state.pop("burn_block", None)
        save_state(round_num, state)

    perform_upload(settings, round_num, output_dir, state, reuse_existing=True)

    if state.get("burn_tx_hash"):
        say(
            f"⏭️  已烧过：tx={str(state['burn_tx_hash'])[:16]}... "
            f"block={state.get('burn_block')}"
        )
    elif not perform_burn(settings, round_num, state):
        return 1

    if not perform_announce(settings, round_num, state):
        return 1

    say("✅ 提交完成。`openroboto status` 查后端有没有收下")
    return 0
