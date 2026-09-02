"""Config: the miner's own `miner.yaml`, plus the `control.json` fetch that
external validators use to pick up `public_key`."""

from __future__ import annotations

from openroboto.config.control import (
    ControlFetch,
    ControlFetchError,
    fetch_control,
)
from openroboto.config.settings import ConfigError, Settings

__all__ = [
    "ConfigError",
    "ControlFetch",
    "ControlFetchError",
    "Settings",
    "fetch_control",
]
