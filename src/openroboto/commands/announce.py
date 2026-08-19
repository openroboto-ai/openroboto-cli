"""`openroboto announce` —— 把提交写上链（旧 `rt.py announce`）。

**burn 之后必须做完这一步**：没有 commitment，那笔 burn 在后端眼里不存在。
payload 的字节由 `openroboto-protocol` 生成，本仓不再拼一份 JSON。
"""

from __future__ import annotations

import argparse
from typing import Any

from openroboto.chain import (
    build_payload,
    get_subtensor,
    open_wallet,
    submit_announcement,
)
from openroboto.config import ConfigError, Settings
from openroboto.console import fail, say
from openroboto.huggingface import commit_sha_from_url
from openroboto.preflight import check_burn_window
from openroboto.round_state import load_state, resolve_round, save_state


def add_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("announce", help="把提交公告写上链")
    parser.add_argument("--config", default="miner.yaml")
    parser.add_argument("--round", type=int, default=0)
    parser.set_defaults(handler=run)


def run(args: argparse.Namespace) -> int:
    settings = Settings.load(args.config)
    round_num = resolve_round(args.round)
    state = load_state(round_num)

    if not perform_announce(settings, round_num, state):
        return 1
    say("   → 用 `openroboto status` 看后端有没有收下这次提交")
    return 0


def perform_announce(settings: Settings, round_num: int, state: dict[str, Any]) -> bool:
    """发 commitment，成功后把 step 标成 announce。"""
    settings.require_for_chain()

    hf_repo_id = str(state.get("hf_repo_id", ""))
    hf_url = str(state.get("hf_url", ""))
    if not hf_repo_id or not hf_url:
        raise ConfigError("断点里没有 HF 仓库信息 —— 先跑 `openroboto upload`")

    # commit SHA 取 URL 里那个（上传返回的就是本次 commit）；URL 不带 commit 段时
    # 退回断点里 `repo_info().sha` 存的那个。旧代码只认 URL，取不到就把 `c` 写成空串 ——
    # 后端拿不到 commit 就没法核对模型，等于烧了 TAO 交了一份废提交。
    hf_commit = commit_sha_from_url(hf_url) or str(state.get("hf_commit", ""))

    subtensor = get_subtensor(settings.network)
    try:
        wallet = open_wallet(settings)
        current_block = subtensor.get_current_block()
        block_hash = subtensor.get_block_hash(current_block)
        say(f"📡 上链 | round={round_num} repo={hf_repo_id} block={current_block}")

        burn_block = int(state.get("burn_block", 0) or 0)
        if not _burn_window_ok(settings, burn_block, current_block):
            return False

        payload = build_payload(
            hotkey_ss58=str(state.get("hotkey_ss58", "")) or wallet.hotkey.ss58_address,
            block_hash=str(block_hash),
            hf_commit=hf_commit,
            round_num=round_num,
            hf_repo_id=hf_repo_id,
            burn_tx_hash=str(state.get("burn_tx_hash", "")),
            burn_block=burn_block,
        )
        result = submit_announcement(subtensor, wallet, settings.netuid, payload)
    finally:
        subtensor.close()

    if not result.ok:
        fail(
            "commitment 没有确认上链。burn 已经发生，**不要重复 burn**。\n"
            "   这有可能只是等待超时而交易其实进了块，所以先查一次："
            "`openroboto status`。\n"
            "   确认后端没收到，再重跑 `openroboto announce`"
            "（断点里的 burn_tx 会复用）"
        )
        return False

    if result.confirmed:
        say(
            f"✅ commitment 已上链 | ref={result.extrinsic_ref} "
            f"fee={result.fee_tao:.6f} TAO"
        )
    else:
        # SDK 报成功但没给区块号（`payment/burn.py:98` 记着这个 SDK 行为）。
        # 不当失败处理 —— 交易确实发出去了；但也不能声称"已上链"。
        say(
            f"✅ commitment 已提交 | fee={result.fee_tao:.6f} TAO\n"
            f"   ⚠️  SDK 没给回区块号，落块情况请用 `openroboto status` 核实"
        )
    state["step"] = "announce"
    state["status"] = "completed"
    save_state(round_num, state)
    return True


def _burn_window_ok(settings: Settings, burn_block: int, current_block: int) -> bool:
    """跑一遍窗口检查并把结论说给矿工听。判定本身在 `preflight` 里（纯函数）。"""
    blocked, warning = check_burn_window(
        burn_block, current_block, settings.burn_block_window
    )
    if blocked:
        fail(blocked)
        return False
    if warning:
        say(f"⚠️  {warning}")
    return True
