"""Fetching and applying control.json — **the only implementation in the repo**.

Before the move, this logic was written out once each in `validator.py` /
`miner.py` / `rt.py`: three User-Agents, three sets of ETag handling, three
different fallbacks on failure. The one in `rt.py` also wrote the `burn_rate_tao`
fallback as 0.01 while production is 0.1 — on a fetch failure the miner would
**burn ten times too little**, the backend would check against the amount and
reject it outright, and the TAO would be gone all the same.

After collapsing this into one place, **not a single fallback value is left**: if
the rate cannot be fetched, then it cannot be fetched, and `commands/burn.py`
refuses to burn (`Settings.burn_rate_tao` defaults to `None`). This module only
refreshes and states the situation clearly; it **does not guess an amount on the
miner's behalf** — the price of guessing wrong is non-refundable TAO.

control.json **carries only payment / dataset / training / process**; it is not a
configuration source for the backend (see openroboto-backend/docs/adr/01).
"""

from __future__ import annotations

import json
import urllib.error
from dataclasses import dataclass
from typing import Any

from openroboto.config.settings import Settings
from openroboto.http_client import build_request, urlopen

FETCH_TIMEOUT_SEC = 30


class ControlFetchError(Exception):
    """control.json could not be fetched / could not be parsed.

    This is an **infrastructure failure**, not a miner misconfiguration. Callers
    must treat it as one: train stops (without a round number there is nothing to
    train), while burn / validator fall back to the local config and carry on.
    """


@dataclass(frozen=True)
class ControlFetch:
    """The result of one fetch."""

    control: dict[str, Any] | None
    """The content of control.json.

    `None` means the server replied 304 and the content is unchanged.
    """

    etag: str
    """This fetch's ETag, sent next time to save bandwidth.

    If the server does not give one, the previous one is kept.
    """


def fetch_control(url: str, etag: str = "") -> ControlFetch:
    """Fetch control.json over HTTP, with ETag conditional request support.

    Args:
        url: the direct link to control.json.
        etag: the ETag from last time; empty means fetch unconditionally.

    Raises:
        ControlFetchError: network error, timeout, or a response that is not a
            valid JSON object.
    """
    request = build_request(url, {"If-None-Match": etag} if etag else None)

    try:
        with urlopen(request, FETCH_TIMEOUT_SEC) as response:
            new_etag = response.headers.get("ETag", "").strip('"') or etag
            # urllib raises HTTPError for 304 (see below); this branch is for the
            # few servers that return a 304 body directly.
            if response.status == 304:
                return ControlFetch(None, new_etag)
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            return ControlFetch(None, etag)
        raise ControlFetchError(
            f"control.json returned HTTP {exc.code}: {url}"
        ) from exc
    except (OSError, urllib.error.URLError) as exc:
        raise ControlFetchError(
            f"Failed to fetch control.json (network problem): {exc}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise ControlFetchError(f"control.json is not valid JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise ControlFetchError(
            "The top level of control.json must be an object, got "
            f"{type(payload).__name__}"
        )
    return ControlFetch(payload, new_etag)


def apply_control(settings: Settings, control: dict[str, Any]) -> None:
    """Overwrite settings with the fields control.json gets to decide for the subnet.

    Only the `payment` and `training` sections change settings:
    - `payment.burn_rate_tao` / `payment.limit_price_rao` — this round's rate, where
      the subnet has the final say;
    - `training.vla_checkpoint_path` / `training.vla_model_id` — the base model.

    The `dataset` and `process` sections are read directly by the train command and
    do not enter settings (they are per-round inputs, not configuration).
    """
    payment = control.get("payment") or {}
    if isinstance(payment, dict):
        if payment.get("burn_rate_tao") is not None:
            settings.burn_rate_tao = float(payment["burn_rate_tao"])
        if payment.get("limit_price_rao") is not None:
            settings.limit_price_rao = int(payment["limit_price_rao"])

    training = control.get("training") or {}
    if isinstance(training, dict):
        if training.get("vla_checkpoint_path"):
            settings.vla_checkpoint_path = str(training["vla_checkpoint_path"])
        if training.get("vla_model_id"):
            settings.vla_model_id = str(training["vla_model_id"])


def refresh_burn_rate(settings: Settings, logger: Any) -> None:
    """Refresh the rate once before burning.

    If it cannot be fetched, keep the current value and say so explicitly.

    Burning too much is not refunded, and burning too little gets rejected by the
    backend on the amount check (also not refunded) — so this log line must let the
    miner see how much they will actually burn. **No fallback value is filled in
    here**: if `burn_rate_tao` is still `None`, `commands/burn.py` refuses to go on
    chain; see this module's docstring.
    """
    if not settings.control_json_url:
        logger.warning(
            "urls.control_json is not configured, so this round's burn rate "
            "cannot be confirmed (payment.burn_rate_tao=%s in miner.yaml). A "
            "mismatched rate is rejected by the backend and not refunded",
            settings.burn_rate_tao,
        )
        return
    try:
        fetched = fetch_control(settings.control_json_url)
    except ControlFetchError as exc:
        logger.warning(
            "Failed to fetch control.json (%s), keeping burn_rate_tao=%s from "
            "miner.yaml",
            exc,
            settings.burn_rate_tao,
        )
        return
    if fetched.control is not None:
        apply_control(settings, fetched.control)
    logger.info("burn_rate_tao=%s TAO (from control.json)", settings.burn_rate_tao)
