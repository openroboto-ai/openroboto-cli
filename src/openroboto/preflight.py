"""烧钱之前的自检。

burn 之后才发现 commitment 发不出去 = TAO 白烧且不退款。所以 `burn` 与 `submit`
都在**花钱之前**跑这一遍：断点里该有的字段齐不齐、payload 编得出来吗、超没超 512 字节。

旧 `rt.py::check_announce_ready` 估算 payload 大小时把 `h`（区块哈希）当成空串，
而真正上链时它是 64 个十六进制字符 —— 也就是**每次都少算 64 字节**。
一个刚好卡在边界上的 hf_repo_id 会顺利通过预检、烧掉 TAO，然后在上链那步炸掉。
这里用一个 64 字符的占位哈希估算，宁可高估。
"""

from __future__ import annotations

from typing import Any

from openroboto_protocol.commitment import (
    CommitmentPayload,
    CommitmentTooLargeError,
    encode,
)

HF_COMMIT_LEN = 40
BLOCK_HASH_PLACEHOLDER = "f" * 64
"""估算用的占位区块哈希，长度与真实值一致。"""

COMMIT_LAG_BLOCKS = 5
"""commitment 从发出到进块的余量估计（区块）。

窗口只剩这么点时先提醒一句：检查是在**当前**区块做的，而 commitment 真正进块
还要再晚几个块，正好卡边界的提交会在后端那边超窗。**不能把它算进阻塞判定** ——
那样就比后端严，会拦掉后端本来会接受的提交。
"""


def check_burn_window(
    burn_block: int, commit_block: int, window: int
) -> tuple[str, str]:
    """burn 与 commitment 的区块距离还在后端窗口内吗？

    返回 `(阻塞原因, 提醒)`，都是空串表示没问题。**纯函数**：不碰链、不打印，
    判定和呈现分开（呈现在 `commands/announce.py`）。

    判定照抄后端 `scanner/burn_verify.py:68-75`，三处细节必须一致，
    否则这个检查会拦住本来能过的提交：

    1. `abs(burn_block - commit_block)`：**对称**，burn 在前在后都算距离；
    2. `> window` 才拒 —— 正好等于窗口是**放行**的；
    3. 任一区块为 0（未知）时后端**整段跳过**这项检查，这里也跳过。
    """
    if burn_block <= 0 or commit_block <= 0:
        return "", ""  # 后端此时也不查，不要比后端更严

    diff = abs(commit_block - burn_block)
    if diff > window:
        return (
            f"burn 距今已经 {diff} 个区块，超出后端窗口 {window} —— "
            f"现在公告上去会被判 `rejected`，而且**已经烧掉的 TAO 不退**。\n"
            f"   burn 在区块 {burn_block}，当前区块 {commit_block}。\n"
            f"   → 这一轮的这笔 burn 作废了。下次 `openroboto submit` 一次跑完，"
            f"或者 burn 完立刻 announce，别隔太久",
            "",
        )

    if diff > window - COMMIT_LAG_BLOCKS:
        return "", (
            f"burn 距今 {diff} 个区块，窗口是 {window} —— 贴着边界了，"
            f"commitment 进块时可能刚好超出。别再等了"
        )
    return "", ""


def check_announce_ready(state: dict[str, Any], round_num: int) -> list[str]:
    """返回阻止提交的原因列表；空列表表示可以往下走。"""
    reasons: list[str] = []

    hf_repo_id = str(state.get("hf_repo_id", ""))
    hf_url = str(state.get("hf_url", ""))
    hf_commit = str(state.get("hf_commit", ""))
    hotkey_ss58 = str(state.get("hotkey_ss58", ""))

    if not hf_repo_id:
        reasons.append("断点里没有 hf_repo_id —— 先跑 `openroboto upload`")
    if not hf_url:
        reasons.append("断点里没有 hf_url —— 先跑 `openroboto upload`")
    if len(hf_commit) != HF_COMMIT_LEN:
        shown = hf_commit[:12] if hf_commit else "空"
        reasons.append(
            f"hf_commit 不合法（{shown}，应为 40 位十六进制）—— "
            "重跑 `openroboto upload`"
        )
    if not hotkey_ss58:
        reasons.append(
            "断点里没有 hotkey_ss58 —— "
            "在 miner.yaml 补 subnet.hotkey_ss58 后重跑 upload"
        )

    if hf_repo_id and hotkey_ss58:
        try:
            payload_size(state, round_num)
        except CommitmentTooLargeError as exc:
            reasons.append(
                f"commitment payload 超长（{exc.size} > 512 字节）—— "
                "换一个更短的 HF 仓库名"
            )

    return reasons


def payload_size(state: dict[str, Any], round_num: int) -> int:
    """预估上链 payload 的字节数（用占位区块哈希与 burn 字段）。"""
    payload = CommitmentPayload(
        hotkey_ss58=str(state.get("hotkey_ss58", "")),
        block_hash=BLOCK_HASH_PLACEHOLDER,
        hf_commit=str(state.get("hf_commit", "")),
        round_num=round_num,
        hf_repo_id=str(state.get("hf_repo_id", "")),
        burn_tx_hash=str(state.get("burn_tx_hash", "")) or "0" * 64,
        burn_block=int(state.get("burn_block", 0) or 0) or 1,
    )
    return len(encode(payload))
