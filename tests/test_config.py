"""配置解析与 control.json 抓取。"""

from __future__ import annotations

import io
import json
import urllib.error
from pathlib import Path
from typing import Any

import pytest

from openroboto.config import (
    ConfigError,
    ControlFetchError,
    Settings,
    apply_control,
    fetch_control,
)
from openroboto.config import control as control_module

MINER_YAML = """
subnet:
  network: finney
  netuid: 80
  coldkey: 123
  hotkey: miner-hot
  hotkey_ss58: 5Miner000000000000000000000000000000000000000000
urls:
  control_json: https://example.invalid/control.json
  dataset_train: https://example.invalid/train.json
huggingface:
  token: hf_xxx
  username: someone
custom_train_script: train_strategy.py
"""


def test_yaml_keys_are_unchanged_from_the_legacy_layout(tmp_path: Path) -> None:
    path = tmp_path / "miner.yaml"
    path.write_text(MINER_YAML, encoding="utf-8")

    settings = Settings.load(str(path))
    assert settings.netuid == 80
    assert settings.coldkey == "123"  # YAML 会把它读成 int，必须转字符串
    assert settings.hotkey == "miner-hot"
    assert settings.control_json_url == "https://example.invalid/control.json"
    assert settings.hf_username == "someone"
    assert settings.custom_train_script == "train_strategy.py"


def test_missing_file_says_how_to_create_one(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as excinfo:
        Settings.load(str(tmp_path / "nope.yaml"))
    assert "openroboto init" in str(excinfo.value)


def test_empty_sections_are_tolerated(tmp_path: Path) -> None:
    path = tmp_path / "miner.yaml"
    path.write_text("subnet:\nurls:\n", encoding="utf-8")
    assert Settings.load(str(path)).network == "finney"


def test_chain_commands_refuse_to_run_without_netuid() -> None:
    """旧代码默认 netuid=313（测试网）。漏配 netuid 会把真钱烧到别的子网上。"""
    settings = Settings()
    assert settings.netuid == 0
    with pytest.raises(ConfigError) as excinfo:
        settings.require_for_chain()
    assert "netuid" in str(excinfo.value)


def test_apply_control_only_touches_payment_and_training() -> None:
    settings = Settings.from_mapping({"backend": {"url": "https://backend.invalid"}})
    apply_control(
        settings,
        {
            "round": 9,
            "status": "active",
            "payment": {"burn_rate_tao": 0.1, "limit_price_rao": 5},
            "training": {"vla_checkpoint_path": "gs://bucket/ckpt", "epochs": 42},
            "dataset": {"train_url": "https://example.invalid/other.json"},
            "public_key": "should-not-land-in-settings",
        },
    )
    assert settings.burn_rate_tao == 0.1
    assert settings.limit_price_rao == 5
    assert settings.vla_checkpoint_path == "gs://bucket/ckpt"
    # dataset / round / public_key 不进 settings —— 它们是每轮的输入，不是配置
    assert settings.dataset_train_url == ""
    assert settings.backend_url == "https://backend.invalid"


class _FakeResponse(io.BytesIO):
    def __init__(self, payload: bytes, status: int = 200, etag: str = "") -> None:
        super().__init__(payload)
        self.status = status
        self.headers = {"ETag": f'"{etag}"'} if etag else {}

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()


def test_fetch_control_returns_payload_and_etag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        control_module,
        "urlopen",
        lambda *a, **k: _FakeResponse(json.dumps({"round": 1}).encode(), etag="abc"),
    )
    fetched = fetch_control("https://example.invalid/control.json")
    assert fetched.control == {"round": 1}
    assert fetched.etag == "abc"


def test_fetch_control_treats_304_as_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """urllib 把 304 当异常抛。这条路径不能被误报成「拉取失败」。"""

    def _raise(*args: Any, **kwargs: Any) -> None:
        raise urllib.error.HTTPError("url", 304, "Not Modified", {}, None)  # type: ignore[arg-type]

    monkeypatch.setattr(control_module, "urlopen", _raise)
    fetched = fetch_control("https://example.invalid/control.json", etag="abc")
    assert fetched.control is None
    assert fetched.etag == "abc"


def test_fetch_control_network_failure_is_infrastructure_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise(*args: Any, **kwargs: Any) -> None:
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(control_module, "urlopen", _raise)
    with pytest.raises(ControlFetchError):
        fetch_control("https://example.invalid/control.json")


def test_refresh_burn_rate_falls_back_to_local_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """control.json 拉不到时，费率退回 miner.yaml —— **不是**退回代码里的字面量。

    旧 `rt.py` 的兜底常量写着 0.01，而线上是 0.1；少烧十倍照样被拒且不退款。
    """
    settings = Settings.from_mapping(
        {
            "urls": {"control_json": "https://example.invalid/control.json"},
            "payment": {"burn_rate_tao": 0.1},
        }
    )

    def _raise(*args: Any, **kwargs: Any) -> None:
        raise urllib.error.URLError("down")

    monkeypatch.setattr(control_module, "urlopen", _raise)

    messages: list[str] = []

    class _Logger:
        def warning(self, message: str, *args: Any) -> None:
            messages.append(message % args)

        def info(self, message: str, *args: Any) -> None:
            messages.append(message % args)

    control_module.refresh_burn_rate(settings, _Logger())
    assert settings.burn_rate_tao == 0.1
    assert any("0.1" in message for message in messages)


# ─── environment：四个必须同源的字段 ─────────────────────────


def test_environment_defaults_urls_but_never_netuid() -> None:
    """环境预设填 URL，**不填 netuid**。

    `netuid` 没有默认值是刻意的：忘了配就该失败，而不是悄悄挑一个子网 ——
    挑错了烧的是真 TAO。环境只负责校验你自己写的那个。
    """
    cfg = Settings.from_mapping({"environment": "dev"})
    assert cfg.network == "test"
    assert "api-dev" in cfg.control_json_url
    assert "api-dev" in cfg.backend_url
    assert cfg.netuid == 0, "环境不许替矿工决定 netuid"


def test_explicit_fields_beat_the_environment_preset() -> None:
    """预设是默认值不是强制值 —— 自建后端的人必须还能用。"""
    cfg = Settings.from_mapping(
        {
            "environment": "dev",
            "backend": {"url": "https://my-own-backend.example"},
        }
    )
    assert cfg.backend_url == "https://my-own-backend.example"
    assert "api-dev" in cfg.control_json_url  # 没被显式覆盖的仍然来自预设


def test_mainnet_netuid_with_dev_control_json_is_refused() -> None:
    """会赔钱的半切换之一：按 dev 的 0.01 费率，在**主网**上烧。

    dev 公布 burn_rate_tao=0.01、生产公布 0.1。这种组合下矿工烧掉十分之一的
    费用，生产后端按金额核对判拒，**且不退款**。
    """
    cfg = Settings.from_mapping(
        {
            "subnet": {"netuid": 80, "network": "finney"},
            "urls": {"control_json": "https://api-dev.openroboto.ai/control.json"},
        }
    )
    with pytest.raises(ConfigError) as excinfo:
        cfg.require_for_chain()
    assert "control_json" in str(excinfo.value)


def test_testnet_netuid_with_mainnet_environment_is_refused() -> None:
    """另一半：提交到测试网，却去问生产要状态 —— `status` 永远空且无从解释。"""
    cfg = Settings.from_mapping({"subnet": {"netuid": 313, "network": "test"}})
    with pytest.raises(ConfigError) as excinfo:
        cfg.require_for_chain()
    assert "313" in str(excinfo.value)


def test_a_coherent_config_passes() -> None:
    """别把正常配置也拦了 —— 比后端严不是安全方向。"""
    Settings.from_mapping(
        {"subnet": {"netuid": 80, "network": "finney"}}
    ).require_for_chain()
    Settings.from_mapping(
        {"environment": "dev", "subnet": {"netuid": 313, "network": "test"}}
    ).require_for_chain()


def test_a_self_hosted_backend_is_not_treated_as_a_conflict() -> None:
    """指向自己搭的后端是合法的，这个检查不该逼所有人用我们的域名。"""
    Settings.from_mapping(
        {
            "subnet": {"netuid": 80, "network": "finney"},
            "backend": {"url": "https://my-own-backend.example"},
            "urls": {"control_json": "https://my-own-backend.example/control.json"},
        }
    ).require_for_chain()


def test_an_unknown_environment_name_fails_instead_of_falling_back() -> None:
    """打错环境名必须报错。悄悄退回 mainnet = 拿真钱做测试。"""
    cfg = Settings.from_mapping({"environment": "staging"})
    with pytest.raises(ConfigError) as excinfo:
        cfg.require_for_chain()
    assert "staging" in str(excinfo.value)


def test_local_environment_accepts_any_backend_url() -> None:
    """自建后端（本地开发、staging、同事的机器）必须能配 —— 否则没法测。"""
    cfg = Settings.from_mapping(
        {
            "environment": "local",
            "subnet": {"netuid": 313, "network": "test"},
            "backend": {"url": "http://localhost:8001"},
            "urls": {"control_json": "http://localhost:8001/control.json"},
        }
    )
    cfg.require_for_chain()
    assert cfg.backend_url == "http://localhost:8001"
    assert cfg.netuid == 313  # local 不约束链，你说了算


def test_local_without_urls_is_refused_rather_than_silently_using_production() -> None:
    """这条是 local 的**要害**。

    不清掉内置默认值的话，`environment: local` 却忘了配 URL 的人会静默连上
    **生产**后端 —— 而他以为自己在本地测。宁可拒绝启动。
    """
    cfg = Settings.from_mapping(
        {"environment": "local", "subnet": {"netuid": 313, "network": "test"}}
    )
    assert cfg.backend_url == "", "local 必须清掉生产默认值"
    assert cfg.control_json_url == ""
    with pytest.raises(ConfigError) as excinfo:
        cfg.require_for_chain()
    assert "local" in str(excinfo.value)


def test_local_pointing_at_a_hosted_environment_is_a_contradiction() -> None:
    """说 local 却填生产地址 —— 不是一套配置，是自相矛盾，得说出来。"""
    cfg = Settings.from_mapping(
        {
            "environment": "local",
            "subnet": {"netuid": 80, "network": "finney"},
            "backend": {"url": "https://api.openroboto.ai"},
            "urls": {"control_json": "https://api.openroboto.ai/control.json"},
        }
    )
    with pytest.raises(ConfigError):
        cfg.require_for_chain()
