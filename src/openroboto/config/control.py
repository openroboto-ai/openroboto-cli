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

## What is left in control.json

**`public_key`, and a `payment` block on its way out.** The file was written
when there was no competitions table, so one static JSON had to carry the whole
round's spec; each of those fields now has a home on the competition row and is
read from there — the round is `seq`, the status is `status`, the dataset and
the base checkpoint are `params.training`, the fee is `params.fee`, and the five
hyperparameters were never the subnet's to set (`miner.yaml`).

`public_key` stays, and this URL must keep answering: it is the only channel an
external validator has for the key it needs to read weights, and we cannot make
those validators upgrade.

It is not a configuration source for the backend either
(see openroboto-backend/docs/adr/01).
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
    must treat it as one: burn / validator fall back to the local config and
    carry on, and `doctor` reports it as one failed check rather than a broken
    setup. `train` is no longer a caller at all.
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
    """Overwrite settings with the one section control.json still decides.

    `payment.burn_rate_tao` / `payment.limit_price_rao` — the subnet-wide rate.

    ⚠️ **Nothing in production calls this any more.** The payment path reads the
    fee off the competition row (`params.fee`), `doctor` shows that fee too, and
    the workspace-with-no-competition-section fallback was removed with
    `openroboto burn` itself. It is kept for its tests and for the moment
    `Settings.burn_rate_tao` is retired with it; do not add a new caller.

    The `training` section used to land here as well
    (`vla_checkpoint_path` / `vla_model_id`), and that is gone: the base
    checkpoint is `params.training.checkpoint` on the competition row, and
    `vla_model_id` had no reader at all — which base model a season runs on is
    `base_model_family`, not a string that says `pi05` for every competition.
    """
    payment = control.get("payment") or {}
    if isinstance(payment, dict):
        if payment.get("burn_rate_tao") is not None:
            settings.burn_rate_tao = float(payment["burn_rate_tao"])
        if payment.get("limit_price_rao") is not None:
            settings.limit_price_rao = int(payment["limit_price_rao"])


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
