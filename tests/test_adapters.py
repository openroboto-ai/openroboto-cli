"""The adapter table: which code path each competition takes.

Two things are guarded here, and both are about what must *not* happen:

- an adapter this client does not know must **not** fall back to the simulation
  default. A miner who believes they are entering the real track while every
  step around them runs the simulation one pays to find out;
- competition **data** must not appear in this file. An image name or a fee
  written here means a release of the CLI to change a number, which is a
  fleet-wide upgrade for one season's parameter.
"""

from __future__ import annotations

import dataclasses
import inspect
import re

import pytest

from openroboto import DEFAULT_RUNNER_PROFILE, adapters, runner_context
from openroboto.config import ConfigError


def test_a_config_from_before_competitions_is_the_pi05_simulation() -> None:
    """The same rule the chain side uses for a commitment with no competition
    id, and the promise MIGRATION.md §2 makes to configs that predate all of
    this."""
    assert adapters.resolve("") is adapters.ADAPTERS[adapters.DEFAULT_ADAPTER]
    assert adapters.format_profile("") == adapters.OPENPI


@pytest.mark.parametrize(
    ("adapter", "training"),
    [
        ("sim_openpi", adapters.DOCKER),
        ("sim_lingbot", adapters.DOCKER),
        ("real_xarm6", adapters.UNAVAILABLE),
    ],
)
def test_each_known_adapter_selects_its_paths(adapter: str, training: str) -> None:
    """Only `training` now. Which rule book judges a checkpoint left this table
    on 2026-08-26: it follows the base model, and `real_xarm6` names a robot
    arm, so this table never knew it -- it guessed, and guessed wrong."""
    assert adapters.resolve(adapter).training == training


@pytest.mark.parametrize(
    ("family", "profile"),
    [("openpi", adapters.OPENPI), ("lingbot_vla", adapters.LINGBOT)],
)
def test_each_base_model_selects_its_format_profile(family: str, profile: str) -> None:
    """🔴 Keys are the backend's vocabulary verbatim; values are directory names
    (`runner/lingbot/`), so `lingbot_vla` maps to `"lingbot"`. Shared strings,
    separate implementations -- not one shared table."""
    assert adapters.FORMAT_PROFILES[family] == profile


@pytest.mark.parametrize(
    ("adapter", "family", "profile"),
    [
        # 🔴 The combination that could not be expressed before: one arm, either
        # base model, and the season row is the only thing that decides.
        ("real_xarm6", "openpi", adapters.OPENPI),
        ("real_xarm6", "lingbot_vla", adapters.LINGBOT),
        # And the season row outranks the adapter name for the sim seasons too.
        ("sim_openpi", "lingbot_vla", adapters.LINGBOT),
        ("sim_lingbot", "openpi", adapters.OPENPI),
    ],
)
def test_the_season_row_decides_the_base_model_not_the_adapter_name(
    adapter: str, family: str, profile: str
) -> None:
    assert adapters.format_profile(adapter, family) == profile


def test_the_real_track_refuses_rather_than_guessing_a_base_model() -> None:
    """🔴 The bug this split removed, kept as a test.

    `real_xarm6` used to carry `format_profile=LINGBOT` in this table -- a guess,
    and backwards: xArm 6 is being brought up on π0.5 first. Nothing could have
    caught it, because the base model is a property of the season and this table
    is keyed by hardware.

    A real-track `miner.yaml` with no `base_model_family` is refused. That is
    also the honest answer today: the season's base model is `null` in the
    database, so there is nothing to copy in yet.
    """
    with pytest.raises(ConfigError) as raised:
        adapters.format_profile("real_xarm6")
    assert "real_xarm6" in str(raised.value)
    assert "robot arm" in str(raised.value)


def test_an_unknown_base_model_refuses_and_does_not_fall_back() -> None:
    """A season naming a base model this client has never heard of is an error,
    never π0.5. Judging a checkpoint by the wrong rules reports a good upload as
    broken -- after the fee is paid."""
    with pytest.raises(ConfigError) as raised:
        adapters.format_profile("sim_openpi", "pi06_next")
    assert "pi06_next" in str(raised.value)


def test_a_legacy_sim_workspace_still_resolves_without_the_new_key() -> None:
    """A `miner.yaml` written before `base_model_family` existed keeps working.

    Only for the two names that provably carry their base model -- the same
    bounded backwards compatibility as `DEFAULT_ADAPTER`, and the reason
    `real_xarm6` is not in that map.
    """
    assert adapters.format_profile("sim_openpi") == adapters.OPENPI
    assert adapters.format_profile("sim_lingbot") == adapters.LINGBOT


def test_an_unknown_adapter_refuses_and_does_not_fall_back() -> None:
    with pytest.raises(ConfigError) as raised:
        adapters.resolve("real_xarm7")
    message = str(raised.value)
    assert "real_xarm7" in message  # what it received
    assert "pip install -U openroboto" in message  # what to do about it
    assert "watch for the announcement" in message  # and if that does not help


def test_an_unknown_adapter_reaches_no_default_by_another_route() -> None:
    """The assertion is the absence of a return value, not the wording of the
    error: a future refactor that "helpfully" defaults would keep the message
    and lose the refusal."""
    with pytest.raises(ConfigError):
        adapters.resolve("real_xarm7")


def test_every_row_of_the_table_is_a_known_path() -> None:
    """The table's own consistency. A typo in a column here dispatches to a
    branch that does not exist, and only for the one competition that uses it."""
    for name, adapter in adapters.ADAPTERS.items():
        assert adapter.training in (adapters.DOCKER, adapters.UNAVAILABLE), name
    for family, profile in adapters.FORMAT_PROFILES.items():
        assert profile in (adapters.OPENPI, adapters.LINGBOT), family


def test_no_adapter_claims_a_container_this_package_does_not_ship() -> None:
    """🔴 `training=DOCKER` is a claim that `openroboto build` has a build
    context for **this competition's base model**, and the base model is the
    format profile.

    This used to assert `format_profile == OPENPI`, because π0.5's was the only
    context in the wheel. Now that `runner/lingbot/` ships too, the assertion
    that still means something is the general one -- a profile with no context
    claiming DOCKER sends `resolve_context()` at a directory that is not there.

    "docker will fail anyway" is not the backstop it sounds like: the image name
    comes from `params.training.image`, so an image under that competition's name
    may well already be on the machine with π0.5 inside it, and the run then
    finishes with no error at all, on the wrong base model.
    """
    for family, profile in adapters.FORMAT_PROFILES.items():
        context = runner_context(profile)
        assert (context / "Dockerfile").is_file(), (
            f"{family}: no build context at {context}"
        )


def test_the_packages_default_runner_profile_is_the_default_adapters() -> None:
    """`openroboto/__init__.py` spells `"openpi"` out rather than importing it
    (importing `adapters` from the package root is a cycle). Two spellings of
    one value, so they are compared here -- if they drift, a `miner.yaml` with
    no competition section builds out of a directory that does not exist."""
    assert DEFAULT_RUNNER_PROFILE == adapters.OPENPI
    assert adapters.format_profile(adapters.DEFAULT_ADAPTER) == DEFAULT_RUNNER_PROFILE


def test_an_adapter_cannot_be_edited_after_it_is_resolved() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        adapters.resolve("sim_openpi").training = adapters.UNAVAILABLE  # type: ignore[misc]


def test_no_competition_data_is_written_into_this_table() -> None:
    """🔴 Addresses and fees are season data. In here they would need a release
    of this package -- and a miner who did not upgrade would pay the old one."""
    source = inspect.getsource(adapters)
    assert re.findall(r"5[A-Za-z0-9]{47}", source) == []
    assert re.findall(r"\d+\.\d+\s*TAO", source) == []
    # Nor is how the fee is paid: that is `params.fee.kind`, and one copy of it.
    assert not any(
        "transfer" in str(value)
        for adapter in adapters.ADAPTERS.values()
        for value in dataclasses.astuple(adapter)
    )
