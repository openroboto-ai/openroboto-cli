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
    """burn 与 commit 的区块距离还在后端窗口内吗？

    照抄后端的判定（`prototype/backend/scanner/burn_verify.py:68-75`），三处细节
    必须一致，否则这个检查会拦住本来能过的提交：

    1. `abs(burn_block - commit_block)`：**对称**，burn 在前在后都算距离；
    2. `> window` 才拒 —— 正好等于窗口是**放行**的；
    3. 任一区块为 0（未知）时后端**整个跳过**这项检查，这里也跳过。

    为什么放在 announce 而不是 `preflight.check_announce_ready()`：那个函数是纯的、
    拿不到链，而这项检查必须知道当前区块。
    """
    if burn_block <= 0 or current_block <= 0:
        return True  # 后端此时也不查，不要比后端更严

    window = settings.burn_block_window
    diff = abs(current_block - burn_block)
    if diff > window:
        fail(
            f"burn 距今已经 {diff} 个区块，超出后端窗口 {window} —— "
            f"现在公告上去会被判 `rejected`，而且**已经烧掉的 TAO 不退**。\n"
            f"   burn 在区块 {burn_block}，当前区块 {current_block}。\n"
            f"   → 这一轮的这笔 burn 作废了。下次 `openroboto submit` 一次跑完，"
            f"或者 burn 完立刻 announce，别隔太久"
        )
        return False

    # commitment 真正进块会比现在再晚几个区块，贴着边界时先提醒一句。
    if diff > window - 5:
        say(
            f"⚠️  burn 距今 {diff} 个区块，窗口是 {window} —— 贴着边界了，"
            f"commitment 进块时可能刚好超出。别再等了"
        )
    return True
