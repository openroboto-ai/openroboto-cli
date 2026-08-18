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
