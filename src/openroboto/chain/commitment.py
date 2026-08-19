"""把一次提交公告写上链。

**payload 的字节由 `openroboto_protocol.commitment.encode()` 产生，本仓不许再写一份。**
后端扫链用同一个模块解码；两边各写一份 JSON 拼装，就是矿工烧了 TAO 没人看见。

上链走 `publish_metadata_extrinsic(data_type="BigRaw")`。`BigRaw` 与 `RawN`
的区别只是**字节长度**（≤128 用 `RawN`），不是客户端版本 —— 见 protocol 包
`commitment.py` 的模块 docstring，那里记着 2026-08 那次「Raw119 是旧客户端」的误判。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from openroboto_protocol.commitment import CommitmentPayload, encode

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SubmitResult:
    """一次 commitment 提交的结果。"""

    ok: bool
    extrinsic_hash: str
    block_height: int
    extrinsic_index: int
    fee_tao: float
    #: 链上**确实**给回了包含这笔 extrinsic 的区块。
    #:
    #: 和 `ok` 不是一回事：`ok` 只说 SDK 认为提交成功，
    #: `confirmed` 说我们拿到了 receipt、知道它落在哪个区块。
    #: 两者分开是因为「以为公告上链了、其实没上」是矿工白烧 TAO 的一条主路径。
    confirmed: bool = False

    @property
    def extrinsic_ref(self) -> str:
        """`区块-序号` 形式的引用，报障时贴这个最省事。

        **没确认就不给区块号。** 以前这里会退回 `get_current_block()`，
        于是未确认的提交也会打印出一个像真的一样的 `6123456-0` ——
        矿工据此认为公告已上链，而它可能根本没进块。
        """
        if not self.confirmed or not self.block_height:
            return "未确认"
        return f"{self.block_height}-{self.extrinsic_index}"


def build_payload(
    *,
    hotkey_ss58: str,
    block_hash: str,
    hf_commit: str,
    round_num: int,
    hf_repo_id: str,
    burn_tx_hash: str,
    burn_block: int,
) -> CommitmentPayload:
    """拼出上链 payload。字段含义与链上键名的对应关系在 protocol 包里定义。"""
    return CommitmentPayload(
        hotkey_ss58=hotkey_ss58,
        block_hash=block_hash,
        hf_commit=hf_commit,
        round_num=round_num,
        hf_repo_id=hf_repo_id,
        burn_tx_hash=burn_tx_hash,
        burn_block=burn_block,
    )


def submit_announcement(
    subtensor: Any, wallet: Any, netuid: int, payload: CommitmentPayload
) -> SubmitResult:
    """把 payload 作为 commitment 发到链上。**等到进块才返回。**

    旧 `utils/chain.py:108-109` 这两个参数都是 `False`，且没有注释说明为什么 ——
    于是「TAO 已经烧掉、公告其实没上链」时命令照样打印成功
    （backend `AGENTS.md` §7 已知缺陷表里的最后一条）。矿工的钱不退，
    所以这里宁可多等一个区块（~12 秒）也要给出真实结论。

    - `wait_for_inclusion=True`：拿到 receipt 才知道落在哪个区块，
      也才让 `SubmitResult.confirmed` 有意义。后端扫链看的就是进块，
      **不需要 finality**。
    - `wait_for_finalization=False`：finality 还要再等 ~30 秒以上，
      对这条业务没有额外价值，纯粹拖长矿工等待。

    超时/RPC 抖动**不报成失败**：extrinsic 可能仍会进块，
    谎报失败会让矿工以为要重来。返回 `ok=False, confirmed=False`，
    由命令层提示先查 `openroboto status`。
    """
    from bittensor.core.extrinsics.serving import publish_metadata_extrinsic

    data = encode(payload)  # 超 512 字节直接抛 CommitmentTooLargeError
    logger.info(
        "上链 commitment | repo=%s round=%d size=%d bytes",
        payload.hf_repo_id,
        payload.round_num,
        len(data),
    )

    try:
        result = publish_metadata_extrinsic(
            subtensor=subtensor,
            wallet=wallet,
            netuid=netuid,
            data_type="BigRaw",
            data=data,
            wait_for_inclusion=True,
            wait_for_finalization=False,
        )
    except (TypeError, AttributeError, NameError):
        # 我们自己写错了（调用签名不对、SDK 改了接口）—— 这时**什么都没发出去**。
        # 报成"结论未知"会让矿工去查 status、等着，而 burn 的 50 个区块窗口
        # 同时在流走：一个我们的 bug 就变成了矿工的一笔 TAO。让它炸出来。
        raise
    except Exception as exc:
        # 基建故障（RPC 断、等待超时），不是矿工的错，也不代表没上链。
        logger.warning("等待 commitment 进块时出错，结论未知：%s", exc)
        return SubmitResult(
            ok=False,
            extrinsic_hash="",
            block_height=0,
            extrinsic_index=0,
            fee_tao=0.0,
            confirmed=False,
        )
    return parse_extrinsic_result(result)


def parse_extrinsic_result(result: Any) -> SubmitResult:
    """把 SDK 返回的 ExtrinsicResponse 解成 `SubmitResult`。

    SDK 在不同版本里返回的形状不一样（有 `extrinsic_receipt` 的、只有布尔的、
    费用字段两个名字的），所以逐个 `getattr` 兜底 —— 这部分的判定顺序沿用旧
    `utils/chain.py::_parse_result`。

    **改掉的一处**：旧实现在拿不到 receipt 时用 `subtensor.get_current_block()`
    填 `block_height`，注释说"只是为了让日志里有个区块号"。但它同时喂给了
    `extrinsic_ref`，于是未确认的提交也会打印出一个看起来完全正常的区块引用。
    现在拿不到 receipt 就是 `confirmed=False`，不编区块号
    （`subtensor` 参数因此不再需要）。
    """
    extrinsic_hash = ""
    block_height = 0
    extrinsic_index = 0
    fee_tao = 0.0

    if result:
        raw_hash = getattr(result, "extrinsic_hash", None)
        if raw_hash is not None:
            extrinsic_hash = (
                raw_hash.hex() if isinstance(raw_hash, bytes) else str(raw_hash)
            )

        receipt = getattr(result, "extrinsic_receipt", None)
        if receipt is not None:
            if extrinsic_hash in ("", "0x"):
                receipt_hash = getattr(receipt, "extrinsic_hash", "")
                extrinsic_hash = (
                    receipt_hash.hex()
                    if isinstance(receipt_hash, bytes)
                    else str(receipt_hash)
                )
            block_height = int(getattr(receipt, "block_number", 0) or 0)
            extrinsic_index = int(getattr(receipt, "extrinsic_idx", 0) or 0)

        fee = getattr(result, "extrinsic_fee", None) or getattr(
            result, "transaction_tao_fee", None
        )
        if fee is not None:
            fee_tao = float(getattr(fee, "tao", 0.0))

    ok = bool(
        getattr(result, "is_success", False)
        or getattr(result, "success", False)
        or result is True
    )
    return SubmitResult(
        ok=ok,
        extrinsic_hash=extrinsic_hash,
        block_height=block_height,
        extrinsic_index=extrinsic_index,
        fee_tao=fee_tao,
        # 有区块号 = receipt 真的回来了。`wait_for_inclusion=True` 下这就是进块。
        confirmed=ok and block_height > 0,
    )
