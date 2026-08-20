"""Config parsing and control.json fetching."""

from __future__ import annotations

import io
import json
import urllib.error
from pathlib import Path
from typing import Any

import pytest
from openroboto_protocol.constants import BURN_BLOCK_WINDOW

from openroboto.config import (
    ConfigError,
    ControlFetchError,
    Settings,
    apply_control,
    fetch_control,
)
from openroboto.config import control as control_module
from openroboto.config.settings import (
    ACTIVITY_CUTOFF_MIN,
    CUTOFF_SAFETY_FACTOR,
    WEIGHT_INTERVAL_FLOOR_MIN,
    check_weight_interval,
)

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
    assert settings.coldkey == "123"  # YAML reads this as an int; it must become a str
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
    """The old code defaulted to netuid=313 (testnet). Forgetting to configure netuid
    burns real money on a different subnet."""
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
    # dataset / round / public_key do not land in settings -- they are per-round inputs,
    # not configuration
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
    """urllib raises 304 as an exception. This path must not be misreported as "fetch
    failed"."""

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
    """When control.json cannot be fetched, the rate falls back to miner.yaml -- **not**
    to a literal in the code.

    The fallback constant in the old `rt.py` said 0.01 while production is 0.1; burning
    ten times too little is still rejected, and still not refunded.
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


# ─── environment: the four fields that must share one source ─────────────────


def test_environment_defaults_urls_but_never_netuid() -> None:
    """The environment preset fills in URLs, and **never netuid**.

    `netuid` having no default is deliberate: forgetting to configure it should fail
    rather than quietly pick a subnet -- picking the wrong one burns real TAO. The
    environment only validates the one you wrote yourself.
    """
    cfg = Settings.from_mapping({"environment": "dev"})
    assert cfg.network == "test"
    assert "api-dev" in cfg.control_json_url
    assert "api-dev" in cfg.backend_url
    assert cfg.netuid == 0, "the environment must not decide netuid for the miner"


def test_explicit_fields_beat_the_environment_preset() -> None:
    """The preset provides defaults, not mandates -- people running their own backend
    must still be able to use it."""
    cfg = Settings.from_mapping(
        {
            "environment": "dev",
            "backend": {"url": "https://my-own-backend.example"},
        }
    )
    assert cfg.backend_url == "https://my-own-backend.example"
    # whatever was not explicitly overridden still comes from the preset
    assert "api-dev" in cfg.control_json_url


def test_mainnet_netuid_with_dev_control_json_is_refused() -> None:
    """One of the half-switched setups that costs money: burning at the dev rate of
    0.01 on **mainnet**.

    dev publishes burn_rate_tao=0.01 and production publishes 0.1. In this combination
    the miner burns one tenth of the fee, the production backend rejects on the amount
    check, **and there is no refund**.
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
    """The other half: submitting to testnet while asking production for status --
    `status` stays empty forever with no explanation available."""
    cfg = Settings.from_mapping({"subnet": {"netuid": 313, "network": "test"}})
    with pytest.raises(ConfigError) as excinfo:
        cfg.require_for_chain()
    assert "313" in str(excinfo.value)


def test_a_coherent_config_passes() -> None:
    """Do not block valid configs too -- being stricter than the backend is not a
    safety direction."""
    Settings.from_mapping(
        {"subnet": {"netuid": 80, "network": "finney"}}
    ).require_for_chain()
    Settings.from_mapping(
        {"environment": "dev", "subnet": {"netuid": 313, "network": "test"}}
    ).require_for_chain()


def test_a_self_hosted_backend_is_not_treated_as_a_conflict() -> None:
    """Pointing at your own backend is legitimate; this check must not force everyone
    onto our domain."""
    Settings.from_mapping(
        {
            "subnet": {"netuid": 80, "network": "finney"},
            "backend": {"url": "https://my-own-backend.example"},
            "urls": {"control_json": "https://my-own-backend.example/control.json"},
        }
    ).require_for_chain()


def test_an_unknown_environment_name_fails_instead_of_falling_back() -> None:
    """A misspelled environment name must fail. Quietly falling back to mainnet means
    testing with real money."""
    cfg = Settings.from_mapping({"environment": "staging"})
    with pytest.raises(ConfigError) as excinfo:
        cfg.require_for_chain()
    assert "staging" in str(excinfo.value)


def test_local_environment_accepts_any_backend_url() -> None:
    """Self-hosted backends (local development, staging, a colleague's machine) must be
    configurable -- otherwise there is no way to test."""
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
    assert cfg.netuid == 313  # local puts no constraint on the chain; you decide


def test_local_without_urls_is_refused_rather_than_silently_using_production() -> None:
    """This is the **critical** one for local.

    If the built-in defaults are not cleared, someone who sets `environment: local` but
    forgets to configure the URLs silently connects to the **production** backend --
    while believing they are testing locally. Better to refuse to start.
    """
    cfg = Settings.from_mapping(
        {"environment": "local", "subnet": {"netuid": 313, "network": "test"}}
    )
    assert cfg.backend_url == "", "local must clear the production defaults"
    assert cfg.control_json_url == ""
    with pytest.raises(ConfigError) as excinfo:
        cfg.require_for_chain()
    assert "local" in str(excinfo.value)


def test_local_pointing_at_a_hosted_environment_is_a_contradiction() -> None:
    """Saying local while filling in production addresses is not a configuration, it is
    a self-contradiction, and it has to be said out loud."""
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


# ─────────────────────────────────────────────────────────────────────────────
# weight_interval_min -- bounded on both sides by the chain
# ─────────────────────────────────────────────────────────────────────────────


def test_weight_interval_default_leaves_real_margin() -> None:
    """The shipped default has to be safe for someone who never touches it.

    It used to be 720 (12 h) against an activity_cutoff of ~16.7 h: one missed
    cycle, or a restart during a deploy, and the validator is past the cutoff.
    Past it, its weights stop counting and the miners it backs earn nothing --
    with no error anywhere, because nothing failed.
    """
    interval = Settings().weight_interval_min

    assert check_weight_interval(interval) == []
    assert interval * CUTOFF_SAFETY_FACTOR <= ACTIVITY_CUTOFF_MIN
    assert interval >= WEIGHT_INTERVAL_FLOOR_MIN


def test_interval_under_the_rate_limit_is_refused() -> None:
    """Below `weights_rate_limit` the extrinsic is rejected and no weights land.

    This is the shape someone lands in by trusting production's config comment,
    which says "(in blocks)" while the code reads minutes: "correcting" 20
    minutes to 20 blocks gives ~4 minutes.
    """
    problems = check_weight_interval(4)

    assert problems and "rate limit" in problems[0]
    # The error has to name the unit, or the next attempt is the same mistake.
    assert "minutes" in problems[0]


def test_interval_too_close_to_the_cutoff_is_refused() -> None:
    """The old default is now refused outright, not merely discouraged."""
    problems = check_weight_interval(720)

    assert problems and "activity_cutoff" in problems[0]
    # Tell them what to use; "too big" alone makes them guess.
    assert str(ACTIVITY_CUTOFF_MIN // CUTOFF_SAFETY_FACTOR) in problems[0]


def test_the_bounds_themselves_are_reachable() -> None:
    """Exactly on the floor and exactly on the ceiling both pass.

    A check that also rejects its own stated bounds sends people hunting for a
    value that satisfies an error message that cannot be satisfied.
    """
    assert check_weight_interval(WEIGHT_INTERVAL_FLOOR_MIN) == []
    assert check_weight_interval(ACTIVITY_CUTOFF_MIN // CUTOFF_SAFETY_FACTOR) == []


def test_require_for_chain_reports_the_interval_with_everything_else() -> None:
    """Reported together with the other problems, not in a second round.

    One command re-run per problem just to learn the next one is the experience
    this check exists to avoid.
    """
    cfg = Settings.from_mapping(
        {"subnet": {"netuid": 80, "network": "finney"}, "weight_interval_min": 720}
    )
    with pytest.raises(ConfigError) as excinfo:
        cfg.require_for_chain()
    assert "activity_cutoff" in str(excinfo.value)


def test_burn_block_window_comes_from_the_protocol_package() -> None:
    """Red line #1: the burn window is installed, never copied into this repo.

    It lived here as a local literal while protocol 0.3.0 was unreleased. The
    release landed on 2026-08-19, so the copy is gone.

    Asserting identity with the protocol constant, not the number 50: a test that
    pins the literal passes just as happily when someone re-forks the value, which
    is the whole thing red line #1 forbids. A window stricter than the backend's
    rejects submissions the backend would have taken -- and the miner has already
    burned by then.
    """
    assert Settings().burn_block_window is BURN_BLOCK_WINDOW
