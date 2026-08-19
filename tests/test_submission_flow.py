"""提交流水线：upload → burn → announce。

这条路上每一步都花钱或不可撤销，所以测的是**顺序与前置条件**：
自检没过一分钱都不烧；已经烧过就不再烧；公告里带的 commit 不能是空的。
链与 HF 全部用假对象，跑这些测试不需要网络、钱包、GPU。
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import pytest
from openroboto_protocol.commitment import CommitmentPayload, decode, encode

from openroboto.chain.commitment import SubmitResult
from openroboto.commands import announce as announce_command
from openroboto.commands import burn as burn_command
from openroboto.commands import submit as submit_command
from openroboto.config import Settings
from openroboto.round_state import save_state

HOTKEY = "5" + "M" * 47
COMMIT = "a" * 40


def _settings() -> Settings:
    return Settings.from_mapping(
        {
            "subnet": {"netuid": 80, "network": "finney", "hotkey_ss58": HOTKEY},
            "payment": {"burn_rate_tao": 0.1},
        }
    )


def _uploaded_state() -> dict[str, Any]:
    return {
        "hf_repo_id": "kyleab/pi05-abcdefghijkl",
        "hf_url": "https://huggingface.co/kyleab/pi05-abcdefghijkl",
        "hf_commit": COMMIT,
        "hotkey_ss58": HOTKEY,
    }


class _FakeSubtensor:
    closed = False

    def get_current_block(self) -> int:
        return 8_888_888

    def get_block_hash(self, block: int) -> str:
        return "0x" + "c" * 64

    def close(self) -> None:
        self.closed = True


class _FakeWallet:
    class hotkey:
        ss58_address = HOTKEY


# ─── burn ────────────────────────────────────────────────────


def test_burn_spends_nothing_when_preflight_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)

    def _explode(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("自检没过还连了链 —— 这就是白烧 TAO 的那条路径")

    monkeypatch.setattr(burn_command, "get_subtensor", _explode)
    monkeypatch.setattr(burn_command, "refresh_burn_rate", lambda *a: None)

    assert burn_command.perform_burn(_settings(), 1, {}) is False


def test_burn_records_tx_and_block_in_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    from openroboto.payment import BurnReceipt

    subtensor = _FakeSubtensor()
    monkeypatch.setattr(burn_command, "get_subtensor", lambda network: subtensor)
    monkeypatch.setattr(burn_command, "open_wallet", lambda settings: _FakeWallet())
    monkeypatch.setattr(burn_command, "refresh_burn_rate", lambda *a: None)

    seen: dict[str, Any] = {}

    def _burn(**kwargs: Any) -> BurnReceipt:
        seen.update(kwargs)
        return BurnReceipt(tx_hash="0x" + "d" * 64, block_number=8_888_880)

    monkeypatch.setattr(burn_command, "execute_stake_burn", _burn)

    state = _uploaded_state()
    assert burn_command.perform_burn(_settings(), 1, state) is True
    assert seen["amount_tao"] == 0.1  # 费率来自配置/control.json，不是代码里的字面量
    assert seen["netuid"] == 80
    assert state["burn_tx_hash"].startswith("0x")
    assert state["burn_block"] == 8_888_880
    assert subtensor.closed


# ─── announce ────────────────────────────────────────────────


def _capture_announcement(
    monkeypatch: pytest.MonkeyPatch, ok: bool = True, confirmed: bool = True
) -> list[CommitmentPayload]:
    captured: list[CommitmentPayload] = []

    def _submit(subtensor: Any, wallet: Any, netuid: int, payload: CommitmentPayload):
        captured.append(payload)
        return SubmitResult(
            ok=ok,
            extrinsic_hash="ff",
            block_height=1,
            extrinsic_index=2,
            fee_tao=0.0,
            confirmed=confirmed,
        )

    monkeypatch.setattr(
        announce_command, "get_subtensor", lambda network: _FakeSubtensor()
    )
    monkeypatch.setattr(announce_command, "open_wallet", lambda settings: _FakeWallet())
    monkeypatch.setattr(announce_command, "submit_announcement", _submit)
    return captured


def test_announce_fills_commit_from_state_when_url_has_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """旧代码只从 hf_url 里抠 commit；URL 不带 `/commit/` 时链上 `c` 就是空串，
    后端拿不到 commit 等于这次提交作废（TAO 已经烧了）。"""
    monkeypatch.chdir(tmp_path)
    captured = _capture_announcement(monkeypatch)

    state = _uploaded_state()
    state["burn_tx_hash"] = "0x" + "d" * 64
    state["burn_block"] = 8_888_880

    assert announce_command.perform_announce(_settings(), 1, state) is True
    payload = captured[0]
    assert payload.hf_commit == COMMIT
    assert payload.burn_block == 8_888_880
    assert state["step"] == "announce"


def test_announce_payload_round_trips_through_the_protocol_codec(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """字节由 protocol 包产生，后端用同一个模块解 —— 这里证明两端对得上。"""
    monkeypatch.chdir(tmp_path)
    captured = _capture_announcement(monkeypatch)

    state = _uploaded_state()
    state["burn_tx_hash"] = "0x" + "d" * 64
    state["burn_block"] = 8_888_880
    announce_command.perform_announce(_settings(), 1, state)

    decoded = decode(encode(captured[0])).payload
    assert decoded.hotkey_ss58 == HOTKEY
    assert decoded.hf_repo_id == "kyleab/pi05-abcdefghijkl"
    assert decoded.burn_tx_hash == "0x" + "d" * 64  # 解码时补回 0x
    assert decoded.block_hash == "c" * 64  # 编码时去掉 0x


def test_announce_failure_tells_the_miner_not_to_burn_again(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    _capture_announcement(monkeypatch, ok=False)

    state = _uploaded_state()
    state["burn_tx_hash"] = "0x" + "d" * 64
    assert announce_command.perform_announce(_settings(), 1, state) is False
    assert "不要重复 burn" in capsys.readouterr().err


# ─── 费率必须是已知的，不许猜（旧默认值 0.01 vs 线上 0.1）──────


def test_burn_refuses_when_the_rate_is_unknown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """control.json 拉不到 → 一分钱都不烧。

    旧代码此时用默认值 0.01 继续烧，而线上费率是 0.1：矿工少烧十倍，
    后端按金额核对判拒，**TAO 不退**。所以这里不能有兜底金额。
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(burn_command, "refresh_burn_rate", lambda *a: None)

    def _explode(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("费率未知还连了链 —— 这正是白烧 TAO 的那条路径")

    monkeypatch.setattr(burn_command, "get_subtensor", _explode)

    settings = Settings.from_mapping({"subnet": {"netuid": 80, "network": "finney"}})
    assert settings.burn_rate_tao is None  # 默认值不许是任何具体金额

    assert burn_command.perform_burn(settings, 1, _uploaded_state()) is False
    assert "拿不到" in capsys.readouterr().err


# ─── burn 生效窗口（后端 50 个区块，超了拒且不退）─────────────
#
# 判定必须和 `prototype/backend/scanner/burn_verify.py:68-75` 一致，
# 严一点就会拦住本来能过的提交。


def _announce_with_burn_block(
    monkeypatch: pytest.MonkeyPatch, burn_block: int, confirmed: bool = True
) -> tuple[bool, list[CommitmentPayload]]:
    captured = _capture_announcement(monkeypatch, confirmed=confirmed)
    state = _uploaded_state()
    state["burn_tx_hash"] = "0x" + "d" * 64
    state["burn_block"] = burn_block
    ok = announce_command.perform_announce(_settings(), 1, state)
    return ok, captured


CURRENT_BLOCK = 8_888_888  # _FakeSubtensor.get_current_block()
WINDOW = 50  # Settings.burn_block_window，线上 backend.yaml 实测值


def test_announce_refuses_once_the_burn_window_has_passed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """超窗口就别再发公告了 —— 后端一定判 rejected，发出去只是白付一笔手续费。"""
    monkeypatch.chdir(tmp_path)
    ok, captured = _announce_with_burn_block(monkeypatch, CURRENT_BLOCK - WINDOW - 1)

    assert ok is False
    assert captured == []  # 一个 commitment 都没发出去
    err = capsys.readouterr().err
    assert "51" in err and "不退" in err  # 说清距离多少、后果是什么


def test_announce_allows_exactly_at_the_window_edge(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """后端是 `block_diff > window` 才拒 —— 正好等于 50 是**放行**的。

    这条边界钉死：写成 `>=` 就会拦掉后端本来会接受的提交。
    """
    monkeypatch.chdir(tmp_path)
    ok, captured = _announce_with_burn_block(monkeypatch, CURRENT_BLOCK - WINDOW)

    assert ok is True
    assert len(captured) == 1


def test_announce_window_check_is_symmetric(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """后端用 `abs(burn_block - commit_block)`，burn 在 commit 之后也算距离。"""
    monkeypatch.chdir(tmp_path)
    ok, captured = _announce_with_burn_block(monkeypatch, CURRENT_BLOCK + WINDOW + 1)

    assert ok is False
    assert captured == []


def test_announce_skips_the_window_check_when_the_burn_block_is_unknown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`burn_block=0` 时后端整段跳过这项检查，我们也跳过 —— 不能比后端更严。"""
    monkeypatch.chdir(tmp_path)
    ok, captured = _announce_with_burn_block(monkeypatch, 0)

    assert ok is True
    assert len(captured) == 1


# ─── 「以为上链了、其实没上」──────────────────────────────────


def test_unconfirmed_submission_does_not_invent_a_block_reference() -> None:
    """没拿到 receipt 就不给区块号。

    旧实现拿不到 receipt 时用 `get_current_block()` 填 `block_height`，
    于是 `extrinsic_ref` 会打印出一个看起来完全正常的 `6123456-0` ——
    矿工据此认为公告已上链，而它可能根本没进块。
    """
    unconfirmed = SubmitResult(
        ok=True,
        extrinsic_hash="ff",
        block_height=CURRENT_BLOCK,  # 就算有值，未确认也不许当成引用
        extrinsic_index=0,
        fee_tao=0.0,
        confirmed=False,
    )
    assert unconfirmed.extrinsic_ref == "未确认"

    confirmed = SubmitResult(
        ok=True,
        extrinsic_hash="ff",
        block_height=CURRENT_BLOCK,
        extrinsic_index=3,
        fee_tao=0.0,
        confirmed=True,
    )
    assert confirmed.extrinsic_ref == f"{CURRENT_BLOCK}-3"


def test_announce_does_not_claim_on_chain_without_a_block_number(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """SDK 报成功但没给区块号：不当失败（交易确实发了），但也不能说"已上链"。"""
    monkeypatch.chdir(tmp_path)
    ok, _ = _announce_with_burn_block(monkeypatch, CURRENT_BLOCK - 8, confirmed=False)

    assert ok is True
    out = capsys.readouterr().out
    assert "已提交" in out and "已上链" not in out


def test_parse_extrinsic_result_confirms_only_with_a_real_block() -> None:
    """`confirmed` 的唯一来源是 receipt 里的区块号，不是 SDK 的成功布尔。"""
    from openroboto.chain.commitment import parse_extrinsic_result

    class _Receipt:
        extrinsic_hash = "0xabc"
        block_number = 8_888_800
        extrinsic_idx = 4

    class _Success:
        is_success = True
        extrinsic_hash = "0xabc"
        extrinsic_receipt = _Receipt()

    class _SuccessNoReceipt:
        is_success = True
        extrinsic_hash = "0xabc"
        extrinsic_receipt = None

    assert parse_extrinsic_result(_Success()).confirmed is True
    assert parse_extrinsic_result(_Success()).extrinsic_ref == "8888800-4"

    no_receipt = parse_extrinsic_result(_SuccessNoReceipt())
    assert no_receipt.ok is True  # 交易发出去了
    assert no_receipt.confirmed is False  # 但不知道在哪个块
    assert no_receipt.block_height == 0  # 不编一个区块号出来


def _payload() -> CommitmentPayload:
    return CommitmentPayload(
        hotkey_ss58=HOTKEY,
        block_hash="c" * 64,
        hf_commit=COMMIT,
        round_num=1,
        hf_repo_id="kyleab/pi05-abcdefghijkl",
        burn_tx_hash="0x" + "d" * 64,
        burn_block=8_888_880,
    )


def _patch_publish(monkeypatch: pytest.MonkeyPatch, fn: Any) -> None:
    import sys
    import types

    fake = types.ModuleType("bittensor.core.extrinsics.serving")
    fake.publish_metadata_extrinsic = fn  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "bittensor.core.extrinsics.serving", fake)


def test_submit_announcement_reports_unknown_on_rpc_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """等待进块时 RPC 断了：结论是"未知"，不是"失败"。

    交易可能仍会进块，谎报失败会让矿工以为要重来。
    """
    from openroboto.chain import commitment as commitment_module

    def _boom(**kwargs: Any) -> None:
        raise TimeoutError("rpc gone")

    _patch_publish(monkeypatch, _boom)

    result = commitment_module.submit_announcement(object(), object(), 80, _payload())
    assert result.ok is False
    assert result.confirmed is False


def test_submit_announcement_does_not_swallow_our_own_bugs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """调用签名写错时必须炸出来，不能报成"结论未知"。

    这时**什么都没发出去**。报"未知"会让矿工去查 status、等着，而 burn 的
    50 个区块窗口同时在流走 —— 我们的一个 bug 变成矿工的一笔 TAO。
    """
    from openroboto.chain import commitment as commitment_module

    def _wrong_signature(**kwargs: Any) -> None:
        raise TypeError("unexpected keyword argument 'data_type'")

    _patch_publish(monkeypatch, _wrong_signature)

    with pytest.raises(TypeError):
        commitment_module.submit_announcement(object(), object(), 80, _payload())


# ─── submit ──────────────────────────────────────────────────


def test_submit_skips_a_finished_round(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    save_state(1, {"step": "announce", "status": "completed"})
    monkeypatch.setattr(
        submit_command.Settings, "load", staticmethod(lambda path: _settings())
    )

    def _explode(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("已完成的轮次不该再走一遍流水线")

    monkeypatch.setattr(submit_command, "perform_upload", _explode)
    args = argparse.Namespace(config="miner.yaml", round=1, output_dir="", force=False)
    assert submit_command.run(args) == 0


def test_submit_reuses_an_existing_burn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """upload 断在网络上再重跑，不能因此多烧一笔。"""
    monkeypatch.chdir(tmp_path)
    state = _uploaded_state()
    state.update({"burn_tx_hash": "0x" + "d" * 64, "burn_block": 8_888_880})
    save_state(2, state)

    monkeypatch.setattr(
        submit_command.Settings, "load", staticmethod(lambda path: _settings())
    )
    monkeypatch.setattr(submit_command, "perform_upload", lambda *a, **k: None)

    def _explode(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("断点里已经有 burn_tx，不该再烧")

    monkeypatch.setattr(submit_command, "perform_burn", _explode)
    monkeypatch.setattr(submit_command, "perform_announce", lambda *a, **k: True)

    args = argparse.Namespace(config="miner.yaml", round=2, output_dir="", force=False)
    assert submit_command.run(args) == 0


def test_submit_force_clears_the_previous_burn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    state = _uploaded_state()
    state.update(
        {"step": "announce", "status": "completed", "burn_tx_hash": "0x" + "d" * 64}
    )
    save_state(3, state)

    monkeypatch.setattr(
        submit_command.Settings, "load", staticmethod(lambda path: _settings())
    )
    monkeypatch.setattr(submit_command, "perform_upload", lambda *a, **k: None)
    burned: list[bool] = []

    def _burn(settings: Settings, round_num: int, state: dict[str, Any]) -> bool:
        burned.append(True)
        state["burn_tx_hash"] = "0x" + "e" * 64
        return True

    monkeypatch.setattr(submit_command, "perform_burn", _burn)
    monkeypatch.setattr(submit_command, "perform_announce", lambda *a, **k: True)

    args = argparse.Namespace(config="miner.yaml", round=3, output_dir="", force=True)
    assert submit_command.run(args) == 0
    assert burned == [True]
