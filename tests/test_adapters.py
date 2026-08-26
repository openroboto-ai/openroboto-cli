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

from openroboto import adapters
from openroboto.config import ConfigError


def test_a_config_from_before_competitions_is_the_pi05_simulation() -> None:
    """The same rule the chain side uses for a commitment with no competition
    id, and the promise MIGRATION.md §2 makes to configs that predate all of
    this."""
    assert adapters.resolve("") is adapters.ADAPTERS[adapters.DEFAULT_ADAPTER]
    assert adapters.resolve("").format_profile == adapters.OPENPI


@pytest.mark.parametrize(
    ("adapter", "profile", "training"),
    [
        ("sim_openpi", adapters.OPENPI, adapters.DOCKER),
        ("sim_lingbot", adapters.LINGBOT, adapters.UNAVAILABLE),
        ("real_xarm6", adapters.LINGBOT, adapters.UNAVAILABLE),
    ],
)
def test_each_known_adapter_selects_its_paths(
    adapter: str, profile: str, training: str
) -> None:
    resolved = adapters.resolve(adapter)
    assert resolved.format_profile == profile
    assert resolved.training == training


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
        adapters.format_profile("real_xarm7")


def test_every_row_of_the_table_is_a_known_path() -> None:
    """The table's own consistency. A typo in a column here dispatches to a
    branch that does not exist, and only for the one competition that uses it."""
    for name, adapter in adapters.ADAPTERS.items():
        assert adapter.format_profile in (adapters.OPENPI, adapters.LINGBOT), name
        assert adapter.training in (adapters.DOCKER, adapters.UNAVAILABLE), name


def test_no_adapter_claims_a_container_this_package_does_not_ship() -> None:
    """🔴 `training=DOCKER` is a claim about `runner/`, and that directory holds
    exactly one Dockerfile, which installs openpi -- `runner/train_runner.py`
    imports `openpi.*` with nothing to fall back on. An adapter judged by another
    rule book claiming DOCKER is that claim made falsely.

    "docker will fail anyway" is not the backstop it sounds like: the image name
    comes from `params.training.image`, so an image under that competition's name
    may well already be on the machine with π0.5 inside it, and the run then
    finishes with no error at all, on the wrong base model.
    """
    for name, adapter in adapters.ADAPTERS.items():
        if adapter.training == adapters.DOCKER:
            assert adapter.format_profile == adapters.OPENPI, name


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
