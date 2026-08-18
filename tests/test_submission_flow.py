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
    monkeypatch: pytest.MonkeyPatch, ok: bool = True
) -> list[CommitmentPayload]:
    captured: list[CommitmentPayload] = []

    def _submit(subtensor: Any, wallet: Any, netuid: int, payload: CommitmentPayload):
        captured.append(payload)
        return SubmitResult(
            ok=ok, extrinsic_hash="ff", block_height=1, extrinsic_index=2, fee_tao=0.0
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
