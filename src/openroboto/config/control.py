"""Fetching control.json — **the only implementation in the repo**.

## What this file is for

`public_key`, and nothing else. It is the only channel an external validator has
for the key it needs to read weights from the backend, and their code is not ours
to upgrade -- so the URL has to keep answering. `commands/validator.py` is the
one caller.

Miners read nothing from it. Everything a season decides -- which competition,
its status, the dataset, the base checkpoint, the entry fee -- is a column on the
competition row, copied into `miner.yaml` by `openroboto init`. The five training
hyperparameters are the miner's own (`miner.yaml`), not the subnet's.

It is not a configuration source for the backend either
(see openroboto-backend/docs/adr/01).
"""

from __future__ import annotations

import json
import urllib.error
from dataclasses import dataclass
from typing import Any

from openroboto.http_client import build_request, urlopen

FETCH_TIMEOUT_SEC = 30


class ControlFetchError(Exception):
    """control.json could not be fetched / could not be parsed.

    This is an **infrastructure failure**, not a miner misconfiguration: the
    validator loop logs it and keeps running rather than exiting, because a
    long-running process must not die because one fetch flapped.
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
