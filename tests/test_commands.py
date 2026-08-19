"""命令层：init / check / build / status / round_state / preflight。

都是纯本地逻辑，不碰网络、不碰 GPU、不碰链。
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any

import pytest
from openroboto_protocol.schemas import Reason, SubmissionHistoryItem

from openroboto.commands import build as build_command
from openroboto.commands import check as check_command
from openroboto.commands import doctor as doctor_command
from openroboto.commands import init as init_command
from openroboto.commands import status as status_command
from openroboto.config import ConfigError, Settings
from openroboto.huggingface import build_repo_id, commit_sha_from_url
from openroboto.preflight import (
    check_announce_ready,
    check_burn_window,
    payload_size,
)
from openroboto.round_state import (
    StateError,
    is_step_done,
    load_state,
    resolve_output_dir,
    resolve_round,
    save_state,
)

BIG_ENOUGH = 11 * 1024 * 1024  # 协议要求整仓 >= 10 MB，低于这个数判「只传了指针」


# ─── init ────────────────────────────────────────────────────


def test_init_releases_config_and_strategy(tmp_path: Path) -> None:
    """矿工全程零 clone：模板打在 wheel 里，init 释放出来。"""
    args = argparse.Namespace(
        directory=str(tmp_path / "my-miner"),
        strategy="simple",
        validator=False,
        force=False,
    )
    assert init_command.run(args) == 0

    target = tmp_path / "my-miner"
    assert (target / "miner.yaml").is_file()
    assert "def train(" in (target / "train_strategy.py").read_text(encoding="utf-8")
    # 模板必须能被自己的解析器读回来
    assert Settings.load(str(target / "miner.yaml")).netuid == 80


def test_init_does_not_overwrite_without_force(tmp_path: Path) -> None:
    config = tmp_path / "miner.yaml"
    config.write_text("subnet:\n  netuid: 80\n", encoding="utf-8")

    args = argparse.Namespace(
        directory=str(tmp_path), strategy="simple", validator=False, force=False
    )
    init_command.run(args)
    assert config.read_text(encoding="utf-8") == "subnet:\n  netuid: 80\n"

    args.force = True
    init_command.run(args)
    assert "OpenRoboto" in config.read_text(encoding="utf-8")


def test_init_validator_writes_validator_config_only(tmp_path: Path) -> None:
    args = argparse.Namespace(
        directory=str(tmp_path), strategy="simple", validator=True, force=False
    )
    init_command.run(args)
    assert (tmp_path / "validator.yaml").is_file()
    assert not (tmp_path / "train_strategy.py").exists()


# ─── check ───────────────────────────────────────────────────


def _make_file(path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    os.truncate(path, size)  # 稀疏文件，不真写 11 MB


def test_check_rejects_a_bare_lora_adapter(tmp_path: Path) -> None:
    """这条路径就是「烧完 TAO 才发现传的是 adapter」的那一次。"""
    _make_file(tmp_path / "adapter_model.safetensors", BIG_ENOUGH)
    (tmp_path / "adapter_config.json").write_text("{}", encoding="utf-8")

    report = check_command.check_directory(tmp_path)
    assert not report.ok
    assert any(issue.code == "bare_lora_adapter" for issue in report.errors)


def test_check_accepts_a_complete_pytorch_checkpoint(tmp_path: Path) -> None:
    _make_file(tmp_path / "model.safetensors", BIG_ENOUGH)
    _make_file(tmp_path / "assets/physical-intelligence/libero/norm_stats.json", 1024)

    report = check_command.check_directory(tmp_path)
    assert report.ok
    assert report.kind == "pytorch"


def test_check_reports_missing_norm_stats(tmp_path: Path) -> None:
    _make_file(tmp_path / "model.safetensors", BIG_ENOUGH)
    report = check_command.check_directory(tmp_path)
    assert not report.ok
    assert any(issue.code == "missing_norm_stats" for issue in report.errors)


def test_check_exit_code_follows_the_report(tmp_path: Path) -> None:
    _make_file(tmp_path / "model.safetensors", BIG_ENOUGH)
    _make_file(tmp_path / "assets/physical-intelligence/libero/norm_stats.json", 1024)
    ok_report = check_command.check_directory(tmp_path)
    assert check_command.report_result(tmp_path, ok_report) == 0


def test_check_on_missing_directory_returns_error(tmp_path: Path) -> None:
    args = argparse.Namespace(path=str(tmp_path / "nope"), round=0)
    assert check_command.run(args) == 1


# ─── build ───────────────────────────────────────────────────


def test_build_prefers_local_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "openpi-runner").mkdir()
    assert build_command.resolve_context() == "openpi-runner"


def test_build_falls_back_to_the_public_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """没有本地目录时用 docker 的 git 上下文 —— 矿工依然不用 clone。

    注意这里用的是真实仓库地址：旧 `scripts/deploy_miner.sh` 的默认值
    `your-org/robot-train-subnet` 根本不存在，照文档跑必然失败。
    """
    monkeypatch.chdir(tmp_path)
    context = build_command.resolve_context()
    assert context.startswith("https://github.com/openroboto-ai/openroboto-cli.git#")
    assert context.endswith(":openpi-runner")
    assert "your-org" not in context


def test_build_command_assembly() -> None:
    assert build_command.build_command("img", "ctx") == [
        "docker", "build", "-t", "img", "ctx",
    ]  # fmt: skip
    assert build_command.build_command("img", "ctx", no_cache=True) == [
        "docker", "build", "-t", "img", "--no-cache", "ctx",
    ]  # fmt: skip


# ─── round_state ─────────────────────────────────────────────


def test_state_round_trip_and_resolution(tmp_path: Path) -> None:
    save_state(3, {"step": "training", "status": "completed"}, base=tmp_path)
    save_state(4, {"step": "prep", "status": "in_progress"}, base=tmp_path)

    assert load_state(3, base=tmp_path)["step"] == "training"
    assert is_step_done(load_state(3, base=tmp_path), "training")
    # 4 号轮没跑完，自动判断要落回 3
    assert resolve_round(0, base=tmp_path) == 3
    assert resolve_round(4, base=tmp_path) == 4


def test_resolve_round_refuses_to_guess(tmp_path: Path) -> None:
    with pytest.raises(StateError) as excinfo:
        resolve_round(0, base=tmp_path)
    assert "--round" in str(excinfo.value)


def test_resolve_output_dir_uses_recorded_path(tmp_path: Path) -> None:
    save_state(1, {"round_output": "/somewhere/round_1"}, base=tmp_path)
    assert resolve_output_dir(1, base=tmp_path) == "/somewhere/round_1"
    assert resolve_output_dir(2, base=tmp_path).endswith("round_2")


def test_corrupt_state_file_reads_as_empty(tmp_path: Path) -> None:
    (tmp_path / "round_1.json").write_text("{ 坏掉的 json", encoding="utf-8")
    assert load_state(1, base=tmp_path) == {}


# ─── preflight ───────────────────────────────────────────────


def _ready_state() -> dict[str, Any]:
    return {
        "hf_repo_id": "someone/pi05-abcdefghijkl",
        "hf_url": "https://huggingface.co/someone/pi05-abcdefghijkl/commit/" + "a" * 40,
        "hf_commit": "a" * 40,
        "hotkey_ss58": "5" + "M" * 47,
    }


def test_preflight_passes_on_a_complete_state() -> None:
    assert check_announce_ready(_ready_state(), 1) == []


def test_preflight_names_every_missing_field() -> None:
    reasons = check_announce_ready({}, 1)
    joined = "\n".join(reasons)
    assert "hf_repo_id" in joined
    assert "hf_url" in joined
    assert "hf_commit" in joined
    assert "hotkey_ss58" in joined


def test_preflight_rejects_a_short_commit_sha() -> None:
    state = _ready_state()
    state["hf_commit"] = "abc"
    assert any("hf_commit" in reason for reason in check_announce_ready(state, 1))


# ─── burn 窗口（纯判定，边界照抄后端）─────────────────────────


def test_burn_window_boundary_matches_the_backend_exactly() -> None:
    """后端是 `abs(diff) > window` 才拒。这三条把边界钉死。

    严一格就会拦掉后端本来会接受的提交；松一格矿工白烧。
    """
    # 正好等于窗口 → 放行（后端用 `>`，不是 `>=`）。
    # 这里只断言"不阻塞"；贴边界的提醒会同时响，那是另一条用例的事。
    assert check_burn_window(1_000, 1_050, 50)[0] == ""

    # 超一个 → 阻塞
    blocked, _ = check_burn_window(1_000, 1_051, 50)
    assert "51" in blocked and "不退" in blocked

    # 对称：burn 在 commit 之后同样算距离
    blocked_reverse, _ = check_burn_window(1_051, 1_000, 50)
    assert blocked_reverse == blocked.replace(
        "burn 在区块 1000，当前区块 1051", "burn 在区块 1051，当前区块 1000"
    )


def test_burn_window_skips_when_either_block_is_unknown() -> None:
    """区块为 0 时后端整段跳过，我们也跳过 —— 不能比后端更严。"""
    assert check_burn_window(0, 1_000, 50) == ("", "")
    assert check_burn_window(1_000, 0, 50) == ("", "")


def test_burn_window_warns_before_the_edge_without_blocking() -> None:
    """贴边界要提醒（commitment 进块还要几个块），但**不能**算进阻塞判定。"""
    blocked, warning = check_burn_window(1_000, 1_048, 50)
    assert blocked == ""  # 48 < 50，后端会接受
    assert "贴着边界" in warning


def test_preflight_size_estimate_includes_the_block_hash() -> None:
    """旧预检把 block_hash 当空串，每次少算 64 字节 —— 边界上的 repo 名会
    通过预检、烧掉 TAO，然后在上链那步炸。"""
    size = payload_size(_ready_state(), 1)
    assert size > 64  # 占位哈希确实进了估算
    assert size <= 512


def test_preflight_blocks_an_oversized_repository_name() -> None:
    state = _ready_state()
    state["hf_repo_id"] = "x" * 500
    reasons = check_announce_ready(state, 1)
    assert any("512" in reason for reason in reasons)


# ─── huggingface 小工具 ──────────────────────────────────────


def test_commit_sha_is_parsed_from_the_commit_url() -> None:
    sha = "b" * 40
    assert commit_sha_from_url(f"https://huggingface.co/u/r/commit/{sha}") == sha
    assert commit_sha_from_url("https://huggingface.co/u/r") == ""


def test_repo_id_follows_the_public_format() -> None:
    settings = Settings.from_mapping({"huggingface": {"username": "kyleab"}})
    address = "5FH32ZXmRZqCuLCS2vhMme6jwznP6NpywGjSqXgcGfvRk2Xp"
    assert build_repo_id(settings, address) == "kyleab/pi05-qXgcGfvRk2Xp"


def test_repo_id_refuses_to_invent_a_fallback() -> None:
    """旧代码在这里退回字面量 `miner`，模型会被传到没人评测的仓库里。"""
    with pytest.raises(ConfigError):
        build_repo_id(Settings(), "")


# ─── status ──────────────────────────────────────────────────


def _history_row(**overrides: Any) -> SubmissionHistoryItem:
    row: dict[str, Any] = {
        "id": 1,
        "task_id": "task-1",
        "uid": 7,
        "hotkey": "5X",
        "round_num": 1,
        "hf_repo_id": "miner/model",
        "hf_commit": "c0ffee",
        "commit_block": 1200,
        "commit_block_timestamp": 1_700_000_000,
        "burn_tx_hash": "0xdead",
        "burn_block": 1180,
        "burn_status": "confirmed",
        "block_hash": "0xbeef",
        "eval_status": "done",
    }
    return SubmissionHistoryItem.model_validate({**row, **overrides})


def test_status_normalises_legacy_words() -> None:
    assert (
        status_command.display_status(_history_row(eval_status="done")) == "evaluated"
    )
    assert (
        status_command.display_status(_history_row(eval_status="failed"))
        == "eval_failed"
    )
    assert status_command.display_status(_history_row(eval_status="")) == "?"


def test_status_round_filter() -> None:
    rows = [_history_row(round_num=1), _history_row(round_num=2)]
    assert status_command._by_round(rows, 0) == rows
    assert status_command._by_round(rows, 2) == [rows[1]]


def test_status_explains_a_rejection_reason() -> None:
    """矿工要的两件事：稳定错误码，以及「还要不要再烧一笔」。"""
    reason = Reason(
        code="BURN_TX_TOO_OLD",
        message="烧的那笔交易太旧了",
        retryable=False,
        source="scan",
    )
    lines = status_command.explain(reason)
    assert "BURN_TX_TOO_OLD" in lines[0]
    assert "重试不会有不同的结果" in lines[1]

    assert status_command.explain(None) == []


def test_status_only_mentions_more_rows_when_there_are_more(
    capsys: pytest.CaptureFixture[str],
) -> None:
    status_command.say_more_hint(False, 3)
    assert capsys.readouterr().out == ""

    status_command.say_more_hint(True, 42)
    assert "42" in capsys.readouterr().out


# ─── doctor ──────────────────────────────────────────────────


def test_doctor_flags_every_field_needed_before_spending() -> None:
    """doctor 是花钱之前的最后一道拦截，缺项必须逐条点名。"""
    results = doctor_command.check_settings(Settings())
    failed = {r.name for r in results if not r.ok}
    assert failed == {"netuid", "hotkey_ss58", "HF 账号", "control.json 地址"}


def test_doctor_passes_on_a_complete_config() -> None:
    settings = Settings.from_mapping(
        {
            "subnet": {"netuid": 80, "hotkey_ss58": "5" + "M" * 47},
            "huggingface": {"username": "someone", "token": "hf_x"},
            "urls": {"control_json": "https://example.invalid/control.json"},
        }
    )
    assert all(result.ok for result in doctor_command.check_settings(settings))


def test_doctor_renders_required_and_optional_differently() -> None:
    required = doctor_command.CheckResult("A", False, "缺", required=True)
    optional = doctor_command.CheckResult("B", False, "缺", required=False)
    assert required.render().startswith("❌")
    assert optional.render().startswith("⚠️")


def test_doctor_python_check_matches_the_supported_floor() -> None:
    assert doctor_command.MIN_PYTHON == (3, 11)  # 矿工侧就是 3.11，不是 3.12
    assert doctor_command.check_python().ok


def test_doctor_control_check_applies_the_rate_it_fetched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`check_control` 必须把抓到的费率**写进 settings**，不只是显示出来。

    `check_wallet` 在它之后跑，靠 `settings.burn_rate_tao` 判断余额够不够。
    只显示不应用的话，余额那项永远报「费率未知」—— 而且不会有人发现。
    """
    from openroboto.config.control import ControlFetch

    monkeypatch.setattr(
        doctor_command,
        "fetch_control",
        lambda url: ControlFetch(
            control={"payment": {"burn_rate_tao": 0.1}}, etag="etag-1"
        ),
    )
    settings = Settings.from_mapping(
        {"urls": {"control_json": "https://example.invalid/control.json"}}
    )
    assert settings.burn_rate_tao is None

    result = doctor_command.check_control(settings)
    assert result.ok
    assert settings.burn_rate_tao == 0.1  # 应用了，不是只印出来
    assert "0.1" in result.detail


def test_doctor_balance_check_does_not_crash_on_an_unknown_rate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """费率未知时不能拿 `None` 去比余额，也不能假装够。

    doctor 是花钱前的自检入口，「查不出来」要如实报成查不出来。
    """

    class _Subtensor:
        def get_balance(self, address: str) -> float:
            return 5.0

        def close(self) -> None:
            pass

    # `check_wallet` 里是惰性 import，所以要打在源模块上。
    import openroboto.chain as chain_module

    monkeypatch.setattr(chain_module, "get_subtensor", lambda network: _Subtensor())
    monkeypatch.setattr(doctor_command, "_coldkey_address", lambda settings: "5abc")

    settings = Settings.from_mapping({"subnet": {"netuid": 80}})
    assert settings.burn_rate_tao is None

    result = doctor_command.check_wallet(settings)
    assert result.ok is False
    assert "未知" in result.detail
