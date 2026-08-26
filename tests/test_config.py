"""Config parsing and control.json fetching."""

from __future__ import annotations

import inspect
import io
import json
import re
import urllib.error
from importlib.resources import files
from pathlib import Path
from typing import Any

import pytest
import yaml
from openroboto_protocol.constants import BURN_BLOCK_WINDOW

from openroboto import adapters
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


#: The five hyperparameters control.json used to hand every miner, verbatim
#: from the production file (`openroboto-backend/tests/fixtures/
#: control_json_baseline.json`). They are the defaults now, so a workspace that
#: does not touch them trains exactly as it did before the move.
CONTROL_JSON_HYPERPARAMETERS = {
    "epochs": 3,
    "batch_size": 4,
    "learning_rate": 1e-4,
    "lora_r": 32,
    "lora_alpha": 64,
}


def _hyperparameters(settings: Settings) -> dict[str, Any]:
    return {name: getattr(settings, name) for name in CONTROL_JSON_HYPERPARAMETERS}


def test_hyperparameter_defaults_are_what_control_json_used_to_serve() -> None:
    assert _hyperparameters(Settings()) == CONTROL_JSON_HYPERPARAMETERS


def test_the_shipped_template_carries_those_same_five_values() -> None:
    """The template writes them out explicitly rather than leaving them to the
    defaults: a miner cannot tune a knob they cannot see.

    Only the `training:` block is parsed here -- the rest of the template holds
    `$placeholders` that `init` fills in from the backend it asked.
    """
    template = (files("openroboto") / "templates" / "miner.yaml").read_text(
        encoding="utf-8"
    )
    written = yaml.safe_load(template)["training"]
    assert written == CONTROL_JSON_HYPERPARAMETERS
    assert (
        _hyperparameters(Settings.from_mapping({"training": written}))
        == CONTROL_JSON_HYPERPARAMETERS
    )


def test_hyperparameters_are_the_miners_to_change(tmp_path: Path) -> None:
    """🔴 The whole point of the move. `learning_rate` is spelled the way the
    template spells it -- YAML 1.1 resolves `1e-4` as a string, so a parser that
    does not cast would ship text to `docker run -e LR=...`."""
    path = tmp_path / "miner.yaml"
    path.write_text(
        "training:\n"
        "  epochs: 10\n"
        "  batch_size: 8\n"
        "  learning_rate: 5.0e-5\n"
        "  lora_r: 64\n"
        "  lora_alpha: 128\n",
        encoding="utf-8",
    )
    assert _hyperparameters(Settings.load(str(path))) == {
        "epochs": 10,
        "batch_size": 8,
        "learning_rate": 5e-5,
        "lora_r": 64,
        "lora_alpha": 128,
    }


def test_a_learning_rate_yaml_read_as_text_still_reaches_the_container_as_a_number(
    tmp_path: Path,
) -> None:
    path = tmp_path / "miner.yaml"
    path.write_text("training:\n  learning_rate: 1e-4\n", encoding="utf-8")
    assert Settings.load(str(path)).learning_rate == 1e-4


def test_apply_control_only_touches_payment() -> None:
    """Everything but `payment` is now read off the competition row instead.

    The `training` block is the one that matters here: while it landed in
    settings, a stale control.json could still decide which base checkpoint a
    season trains from -- the exact override this move exists to remove.
    """
    settings = Settings.from_mapping(
        {
            "backend": {"url": "https://backend.invalid"},
            "model": {"vla_checkpoint_path": "/mine/pi05_base"},
            "training": {"epochs": 7},
        }
    )
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
    # round / status / dataset / training / public_key change nothing.
    assert settings.vla_checkpoint_path == "/mine/pi05_base"
    assert settings.epochs == 7
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


def test_a_season_from_one_backend_is_not_paid_for_on_another_chain() -> None:
    """🔴 **Every self-describing field agrees, and the config is still wrong.**

    This is the workspace `init --backend-url <a local backend>` used to write:
    the season came from 127.0.0.1:8011, and everything written around it says
    mainnet -- environment, network, netuid, both URLs, all consistent with each
    other. The contradiction is not among them, which is exactly why it was
    never caught: it is between the file and where the season came from.

    What it costs is not theoretical. `(track, seq)` is unique inside one
    database, not across them, and both sides seed the same tracks -- so
    `submit` looks up `(sim, 2)`, **finds** production's `(sim, 2)`, and burns
    real TAO for a season this workspace was never trained for.
    """
    cfg = Settings.from_mapping(
        {
            "environment": "mainnet",
            "subnet": {"netuid": 80, "network": "finney"},
            "competition": {
                "track": "sim",
                "seq": 2,
                "source": "http://127.0.0.1:8011",
            },
        }
    )
    with pytest.raises(ConfigError) as excinfo:
        cfg.require_for_chain()
    assert "127.0.0.1:8011" in str(excinfo.value)


def test_the_same_host_on_another_port_is_another_backend() -> None:
    """Two backends on one machine differ by nothing but the port, and the
    dev default (8001) is one keystroke from the one in the report (8011)."""
    cfg = Settings.from_mapping(
        {
            "environment": "local",
            "subnet": {"netuid": 313, "network": "test"},
            "backend": {"url": "http://127.0.0.1:8001"},
            "urls": {"control_json": "http://127.0.0.1:8001/control.json"},
            "competition": {
                "track": "sim",
                "seq": 2,
                "source": "http://127.0.0.1:8011",
            },
        }
    )
    with pytest.raises(ConfigError):
        cfg.require_for_chain()


def test_a_workspace_that_names_its_own_backend_passes() -> None:
    """The ordinary case, and the one `init` writes: the season came from the
    backend this config talks to. Being stricter than that would refuse every
    workspace the command produces."""
    Settings.from_mapping(
        {
            "subnet": {"netuid": 80, "network": "finney"},
            "competition": {
                "track": "sim",
                "seq": 1,
                "source": "https://api.openroboto.ai",
            },
        }
    ).require_for_chain()


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


# ─── competition ─────────────────────────────────────────────


def test_competition_section_is_read_and_passed_through_verbatim() -> None:
    """`init` writes the competition it was told to; the CLI dispatches on the
    adapter and hands `params` on untouched.

    The pass-through is the point: a competition parameter the client has never
    heard of must survive into the code that needs it, or every new parameter
    costs a CLI release and a fleet-wide upgrade.
    """
    cfg = Settings.from_mapping(
        {
            "competition": {
                "adapter": "sim_lingbot",
                "params": {
                    "fee": {"burn_rate_tao": 0.1},
                    "format": {"cameras": ["camera_top"], "unknown_key": 7},
                },
            }
        }
    )
    assert cfg.competition_adapter == "sim_lingbot"
    assert cfg.competition_params["format"]["unknown_key"] == 7
    assert cfg.competition_params["fee"] == {"burn_rate_tao": 0.1}


def test_a_config_without_a_competition_section_still_parses() -> None:
    """Every miner.yaml written before competitions existed. MIGRATION.md §2
    promises it keeps working; `adapters.DEFAULT_ADAPTER` decides what it means."""
    cfg = Settings.from_mapping({"subnet": {"netuid": 80}})
    assert cfg.competition_adapter == ""
    assert cfg.competition_params == {}


def test_competition_params_must_be_a_mapping() -> None:
    with pytest.raises(ConfigError):
        Settings.from_mapping({"competition": {"adapter": "x", "params": ["nope"]}})


def test_the_config_carries_the_base_model_through_to_the_rule_book() -> None:
    """🔴 End to end for the key this whole split added: `miner.yaml` says which
    base model, and that is what picks the rule book -- **not** the adapter name.

    The pair below is the point: same adapter, two base models, two profiles.
    The previous version of this test asserted `real_xarm6 == LINGBOT`, which was
    a guess baked into the table, and backwards from the plan (xArm 6 comes up on
    π0.5 first).
    """
    for family, profile in (
        ("openpi", adapters.OPENPI),
        ("lingbot_vla", adapters.LINGBOT),
    ):
        cfg = Settings.from_mapping(
            {"competition": {"adapter": "real_xarm6", "base_model_family": family}}
        )
        assert cfg.competition_base_model_family == family
        assert (
            adapters.format_profile(
                cfg.competition_adapter, cfg.competition_base_model_family
            )
            == profile
        )


def test_a_config_with_no_base_model_falls_back_only_where_it_is_provable() -> None:
    """The two sim adapters provably name their base model, so a `miner.yaml`
    written before this key existed keeps working. `real_xarm6` names hardware,
    so it is refused instead -- that is the whole reason the key exists."""
    assert adapters.format_profile("sim_openpi") == adapters.OPENPI
    assert adapters.format_profile("sim_lingbot") == adapters.LINGBOT
    assert adapters.format_profile("") == adapters.OPENPI
    with pytest.raises(ConfigError):
        adapters.format_profile("real_xarm6")


def test_an_unknown_adapter_is_refused_rather_than_treated_as_simulation() -> None:
    """🔴 The failure this exists to prevent: a competition this client is too old
    to know about, silently judged by the π0.5 rules. That verdict is delivered to
    the miner as "no model weights found" right before they decide whether to burn.
    """
    with pytest.raises(ConfigError) as excinfo:
        adapters.resolve("real_xarm7")
    message = str(excinfo.value)
    assert "real_xarm7" in message
    assert "pip install -U openroboto" in message


def test_the_adapter_table_holds_no_competition_data() -> None:
    """Values (images, templates, fees, addresses) come from `competition.params`.
    One written into this table means a CLI release to change a number."""
    source = inspect.getsource(adapters)
    assert not re.search(r"5[A-Za-z0-9]{47}", source)
    assert not re.search(r"\d+\.\d+\s*TAO", source)
