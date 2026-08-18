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

    @property
    def extrinsic_ref(self) -> str:
        """`区块-序号` 形式的引用，报障时贴这个最省事。"""
        if not self.block_height:
            return "N/A"
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
    """把 payload 作为 commitment 发到链上。"""
    from bittensor.core.extrinsics.serving import publish_metadata_extrinsic

    data = encode(payload)  # 超 512 字节直接抛 CommitmentTooLargeError
    logger.info(
        "上链 commitment | repo=%s round=%d size=%d bytes",
        payload.hf_repo_id,
        payload.round_num,
        len(data),
    )

    result = publish_metadata_extrinsic(
        subtensor=subtensor,
        wallet=wallet,
        netuid=netuid,
        data_type="BigRaw",
        data=data,
        wait_for_inclusion=False,
        wait_for_finalization=False,
    )
    return parse_extrinsic_result(result, subtensor)


def parse_extrinsic_result(result: Any, subtensor: Any) -> SubmitResult:
    """把 SDK 返回的 ExtrinsicResponse 解成 `SubmitResult`。

    SDK 在不同版本里返回的形状不一样（有 `extrinsic_receipt` 的、只有布尔的、
    费用字段两个名字的），所以逐个 `getattr` 兜底。这段是原样搬旧
    `utils/chain.py::_parse_result` 的判定顺序，没有改。
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

    if block_height == 0:
        try:
            block_height = int(subtensor.get_current_block())
        except Exception as exc:  # 只是为了让日志里有个区块号，失败不影响结论
            logger.debug("取当前区块失败：%s", exc)

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
    )
