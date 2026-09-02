"""Read-only client for the backend API.

Uses only the stdlib `urllib` -- making miners install `requests` for the sake
of one GET is not worth it.

## Response shape: one envelope, success and failure differ structurally

```jsonc
// Success (data is an array on list endpoints; pagination lives in meta.page,
// it is not mixed into data)
{"data": [ … ],
 "meta": {"request_id": "01H…", "generated_at": "2026-08-18T06:37:34Z",
          "page": {"total": 7, "limit": 50, "offset": 0, "has_more": false}}}

// Failure: **no data**
{"error": {"code": "BURN_TX_TOO_OLD", "message": "…", "retryable": false},
 "meta": {"request_id": "01H…", "generated_at": "…"}}
```

For miners, this answers three things:

- it is `data` or `error`, one of the two -- no need to memorize conventions
  like "`code: 0` means success" that exist only in the documentation;
- `error.retryable` directly answers the only question that really matters:
  **whether to burn another TAO and retry.** Infrastructure flapping is
  `true`; "your model format is wrong" is `false` -- retrying the latter a
  hundred times gives the same result every time;
- `error.code` is a stable machine code. Wording changes and gets translated,
  the code does not; scripts must branch on it and nothing else.

On failure `meta.request_id` is printed along with the error. Paste that line
when reporting a problem and we can pull up every log of that request
directly, without having to ask each other "when did you run it, and what
exactly did you type".

The field models are all installed from `openroboto-protocol` (envelope,
submission records, rejection records); **this repo does not keep a copy**:
once both sides pin the same version, "the shape the backend sends" and "the
shape the CLI parses" are **one and the same declaration**, not a verbal
agreement transcribed twice.

## Which endpoints need a key (measured 2026-08-17, api.openroboto.ai)

| Endpoint | Without a key |
|---|---|
| `GET /api/v1/scan-rejections` | 200 -- miners look up rejection reasons here |
| `GET /api/v1/submissions/history` | 200 |
| `GET /api/weights` | 401, validators send `X-API-Key: <public_key>` |
| `GET /api/miner/{hotkey}` | 401 |

So `openroboto status` goes through the first two: **a miner needs no key at
all to query their own submissions**.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
from datetime import datetime
from typing import Any, TypeVar

from openroboto_protocol.schemas import (
    Competition,
    Contract,
    ErrorEnvelope,
    ListEnvelope,
    ScanRejection,
    SubmissionHistoryItem,
    Weights,
)

from openroboto.http_client import build_request, urlopen

#: 🔴 **Without sending this header you get no envelope at all.**
#:
#: The backend's default shape is bare JSON (during the migration, so as not
#: to break the evaluation workers; see backend ADR 02 §6); it only returns
#: `{data, meta}` / `{error: {...}, meta}` when explicitly asked. Dropping
#: this line looks like this: none of the envelope-parsing code in this file
#: **is ever triggered even once** -- `_error_envelope()` always returns None,
#: `data` is never obtainable, and **nothing raises an error**; you just get
#: data in the wrong shape.
#:
#: The trailing `application/json` is there for gateways in the middle and for
#: backends that have not been upgraded yet: with only the vendor type, some
#: proxies answer 406. The backend's negotiation is a substring match and
#: prefers the envelope, so listing both does not stop it from returning the
#: envelope.
ACCEPT = "application/vnd.openroboto.envelope+json, application/json"

REQUEST_TIMEOUT_SEC = 30
DEFAULT_LIMIT = 20

HISTORY_PATH = "/api/v1/submissions/history"
REJECTIONS_PATH = "/api/v1/scan-rejections"
COMPETITIONS_PATH = "/api/v1/competitions"
HEALTH_PATH = "/healthz"
WEIGHTS_PATH = "/api/v1/weights"
#: The pre-v1 address. Still live, and still the only one an
#: un-migrated backend has -- see `fetch_weights`.
WEIGHTS_PATH_LEGACY = "/api/weights"

KEY_HINT = (
    "\n  This endpoint needs an API key -- validators copy public_key from "
    "control.json into backend.public_key in validator.yaml"
)

#: How many lines of the raw error to quote at most when the shape does not
#: match: pydantic lists a problem for **every single row**, so a page of 20
#: can scroll hundreds of lines, while the first few are already enough to see
#: which field does not match.
MAX_MISMATCH_LINES = 6

_Model = TypeVar("_Model", bound=Contract)


def retry_advice(retryable: bool) -> str:
    """The wording for "should I retry" exists **only here**.

    Both `error.retryable` from the error envelope and `reason.retryable` from
    a rejection record use it. If the two places said different things, miners
    would have to guess "do I really have to burn another one or not".
    """
    if retryable:
        return "This is a temporary failure; retrying it as-is usually works."
    return "Retrying will not give a different result -- fix the reason above first."


class BackendError(Exception):
    """A backend request failed.

    Carries the three things a miner needs to diagnose it themselves: the
    stable error code, whether it can be retried, and the request_id.
    `__str__` expands them into a few lines of plain language -- `cli.py`
    prints that string directly and does not need a renderer of its own.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str = "",
        retryable: bool = False,
        request_id: str = "",
    ) -> None:
        super().__init__(message)
        #: Stable machine code. Script authors are only allowed to branch on
        #: this.
        self.code = code
        #: Whether retrying is meaningful. For the wording see
        #: `retry_advice()`.
        self.retryable = retryable
        #: Paste this to us when reporting a problem; one line is enough for
        #: us to pull the logs of the whole request.
        self.request_id = request_id

    def __str__(self) -> str:
        lines = [str(self.args[0]) if self.args else ""]
        if self.code:
            lines.append(f"  error code: {self.code}")
        lines.append(f"  {retry_advice(self.retryable)}")
        if self.request_id:
            lines.append(
                f"  request_id: {self.request_id} -- send us this line when "
                f"reporting a problem"
            )
        return "\n".join(lines)


def fetch_submissions(
    base_url: str,
    competition: int,
    hotkey: str = "",
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
) -> ListEnvelope[SubmissionHistoryItem]:
    """Query one competition's submission history.

    🔴 **`competition` is required by the backend**, and is a competition **id**
    -- omitted, the endpoint answers 422 rather than picking a season. That is
    deliberate on its side: a default would give "which season is this response
    about" two answers, the one that was asked for and the one that was not.

    The filter is applied on the **server**. The season is not in the response
    at all, so there would be nothing left to filter on once the rows arrive.

    Returns the whole envelope instead of just the rows: `meta.page.has_more`
    is the only reliable answer to "you have submissions that were not
    displayed". If callers computed `offset + len(rows) < total` themselves,
    that expression would have to be written once per list endpoint, and
    getting it wrong once shows up as **silently displaying a few rows too
    few** -- neither the backend nor the CLI raises any error.
    """
    raw = _get(
        base_url,
        HISTORY_PATH,
        {
            "competition": competition,
            "hotkey": hotkey,
            "limit": limit,
            "offset": offset,
        },
    )
    return _parse(ListEnvelope[SubmissionHistoryItem], raw, HISTORY_PATH)


def fetch_rejections(
    base_url: str,
    hotkey: str = "",
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
) -> ListEnvelope[ScanRejection]:
    """Query records rejected during the chain-scan stage -- the answer to
    "it is on chain but not in the queue" is here.

    🔴 **Not filtered by competition, deliberately.** These rows are rejections
    from *before* admission, so they have no season attached; the endpoint can
    only filter on the ordinal the miner put in their own payload. Filtering a
    rejection list by a number the rejected payload may itself have got wrong
    hides exactly the row the miner came here to find.
    """
    raw = _get(
        base_url,
        REJECTIONS_PATH,
        {"hotkey": hotkey, "limit": limit, "offset": offset},
    )
    return _parse(ListEnvelope[ScanRejection], raw, REJECTIONS_PATH)


def fetch_competitions(
    base_url: str, *, include_archived: bool = False
) -> ListEnvelope[Competition]:
    """The competitions taking submissions, in `(track, seq)` order.

    Anonymous: a miner who has just run `pip install openroboto` holds no key,
    and this is their **first** call to the backend. Sorted by the backend and
    not re-sorted here -- "there is only one, so do not ask" in `init` depends
    on that order being the backend's, not on whatever a local sort happens to
    produce.

    ⚠️ The parameter is `include_archived`, a bool. An `?archived=1` invented
    here would be dropped by FastAPI as an undeclared query string: the archived
    season simply never comes back, **and nothing reports an error**.
    """
    raw = _get(
        base_url,
        COMPETITIONS_PATH,
        # Sent only when asked for. `False` would go out as the string "False",
        # which happens to parse correctly today and is one backend refactor
        # away from not doing so.
        {"include_archived": "true" if include_archived else ""},
    )
    return _parse(ListEnvelope[Competition], raw, COMPETITIONS_PATH)


def fetch_netuid(base_url: str) -> int:
    """Which subnet this backend watches, from its own liveness probe.

    For a self-hosted backend this is the **only** honest answer to "which chain
    is this workspace on". `openroboto init` used to answer it from a static
    template instead, which is how asking a testnet backend for the season
    produced a mainnet workspace around it.

    ⚠️ `/healthz` is deliberately **not** enveloped (backend ADR 02 §3.3: probes
    stay bare JSON so orchestrators can read fixed field paths), so this is the
    one endpoint here that is parsed by hand rather than by the protocol package.

    Raises:
        BackendError: unreachable, or it does not say. **Never returns a guess** --
            a netuid invented here is the one number that decides which chain
            burns the fee.
    """
    try:
        raw = _get(base_url, HEALTH_PATH)
    except BackendError as exc:
        raise BackendError(
            f"{base_url} answered the competition list but not {HEALTH_PATH}, so "
            f"it cannot say which subnet it watches -- and that is what decides "
            f"where your fee is burned. Nothing was written.\n"
            f"  {exc.args[0] if exc.args else exc}",
            code=exc.code,
            retryable=exc.retryable,
            request_id=exc.request_id,
        ) from exc

    body = _decode(raw, HEALTH_PATH)
    netuid = body.get("netuid") if isinstance(body, dict) else None
    if not isinstance(netuid, int) or isinstance(netuid, bool) or netuid <= 0:
        raise BackendError(
            f"{base_url}{HEALTH_PATH} reports netuid {netuid!r}, which is not a "
            f"subnet number.\n"
            f"  → upgrade that backend, or point --backend-url at one that "
            f"answers the probe"
        )
    return netuid


class RosterEntry(Contract):
    """One row of a competition's entry list.

    ⚠️ **The only response model in this repository that the protocol package
    does not publish.** `openroboto-protocol` 0.9.0 has `Competition` but still
    no roster model, and it is released -- adding one means a release of that
    package plus a re-pin here. This is a display-only path (`openroboto
    status`), no money branches on it, so it waits here for the protocol
    package's next version rather than blocking the command. **Do not grow this
    habit**: every other model comes from the protocol package precisely so that
    "the shape the backend sends" and "the shape the CLI parses" are one
    declaration.

    The field name is `payment_status`, not `burn_status`. The new endpoints use
    the real name (a fee can be a transfer); the older endpoints still say
    `burn_*`, and the two must not be confused for each other.
    """

    hotkey: str
    #: Nullable: production has nine rows at `uid=0` for nine different hotkeys,
    #: which means "not known", not "uid zero".
    uid: int | None = None
    hf_repo_id: str = ""
    hf_commit: str | None = None
    #: The moment it was announced **on chain**, not the moment it was written
    #: to the database (a re-scan rewrites the latter).
    submitted_at: datetime | None = None
    #: Verbatim from the backend, one of the eight payment status words. Not
    #: mapped onto a second vocabulary here.
    payment_status: str = ""
    hf_access_status: str = ""
    invalid_reason: str | None = None
    #: Whether this row still occupies the `(hotkey, competition, hf_commit)`
    #: slot -- i.e. whether submitting that commit again would be skipped.
    #:
    #: 🔴 **Required, deliberately, on the one model in this file that has
    #: defaults for everything else.** It is a conclusion the backend computes
    #: (`submission_writes.counts_as_submitted`), and it is what `submit` asks
    #: before it pays; a default here would answer "the slot is free" on behalf
    #: of a backend that never said so, which is the direction that spends the
    #: fee. A backend too old to send it therefore fails to parse -- loudly, with
    #: `_parse`'s "the backend has not caught up yet" -- rather than being
    #: guessed at.
    counts_as_submitted: bool


def fetch_roster(
    base_url: str,
    competition_id: int,
    *,
    hotkey: str = "",
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
) -> ListEnvelope[RosterEntry]:
    """One competition's entry list, newest submission first.

    `hotkey` filters it down to one miner, which is how "am I on the list" is
    answered without paging through everyone else.
    """
    path = f"{COMPETITIONS_PATH}/{competition_id}/roster"
    raw = _get(base_url, path, {"hotkey": hotkey, "limit": limit, "offset": offset})
    return _parse(ListEnvelope[RosterEntry], raw, path)


def fetch_weights(base_url: str, public_key: str = "") -> Weights:
    """Fetch the current weights `{hotkey: share}`. Used by validators,
    requires public_key.

    Prefers `/api/v1/weights` and falls back to `/api/weights`. The fallback is
    not defensiveness about a hypothetical: production runs the pre-v1 backend
    today, and external validators point at it. Removing the fallback is safe
    only once no reachable backend serves the old address alone.

    **This endpoint accepts both shapes** (the envelope's `data`, and a bare
    `{hotkey: share}`): ADR 02 §8.5 explicitly says whether `/api/weights`
    should be wrapped in an envelope "is recommended to be decided
    separately", and it has not been decided to this day. The cost of
    guessing wrong is asymmetric -- failing to parse the weights → no
    `set_weights` can be sent → **emissions across the whole network stop
    silently**, with nothing but a single warning line in the log. Accepting
    one extra shape is two lines of code.
    """
    try:
        raw = _get(base_url, WEIGHTS_PATH, api_key=public_key)
        path = WEIGHTS_PATH
    except BackendError:
        # Fall back rather than fail. The backend being replaced serves only the
        # pre-v1 address, and it is what production is running right now, so a
        # validator that insisted on v1 would stop setting weights the moment it
        # was upgraded ahead of the backend. Emissions stopping is the failure
        # this whole function is written to avoid.
        raw = _get(base_url, WEIGHTS_PATH_LEGACY, api_key=public_key)
        path = WEIGHTS_PATH_LEGACY

    body = _decode(raw, path)
    data = body.get("data", body) if isinstance(body, dict) else body
    # v1 wraps it once more: {"data": {"weights": {...}}, "meta": {...}}.
    if isinstance(data, dict) and "weights" in data:
        data = data["weights"]
    if not isinstance(data, dict):
        raise BackendError(f"{path} returned {type(data).__name__}, expected an object")
    return {str(k): float(v) for k, v in data.items() if isinstance(v, (int, float))}


def _get(
    base_url: str,
    path: str,
    params: dict[str, Any] | None = None,
    api_key: str = "",
) -> bytes:
    """GET an endpoint and return the raw response body. Empty-valued
    parameters are not sent.

    Returns bytes instead of a parsed object: the envelope is parsed by
    pydantic straight from JSON (`model_validate_json`), which saves the
    "json.loads first, then feed it into the model" round trip.
    """
    query = {k: str(v) for k, v in (params or {}).items() if v not in ("", None)}
    url = f"{base_url.rstrip('/')}{path}"
    if query:
        url = f"{url}?{urllib.parse.urlencode(query)}"

    headers = {"Accept": ACCEPT}
    if api_key:
        headers["X-API-Key"] = api_key
    request = build_request(url, headers)

    try:
        with urlopen(request, REQUEST_TIMEOUT_SEC) as response:
            raw: bytes = response.read()
    except urllib.error.HTTPError as exc:
        # HTTPError is itself the response object, and **the envelope is in
        # its body**. Not reading it means throwing away code / retryable /
        # request_id entirely, leaving nothing but a bare status code.
        raise _http_failure(url, exc) from exc
    except (OSError, urllib.error.URLError) as exc:
        raise BackendError(
            f"Cannot reach the backend {url}: {exc}", retryable=True
        ) from exc

    # Envelope rule: a success always has data and never has error. An error
    # inside a 200 is a backend bug, but when it does happen it must still be
    # reported as an error -- passing an error downstream as business data is
    # a silent failure.
    failure = _error_envelope(raw)
    if failure is not None:
        raise _from_envelope(failure)
    return raw


def _http_failure(url: str, exc: urllib.error.HTTPError) -> BackendError:
    """Turn a 4xx / 5xx into an error a miner can act on."""
    hint = KEY_HINT if exc.code == 401 else ""

    # An HTTPError whose fp is None has no readable body (the connection is
    # already closed, or the error was constructed by hand).
    failure = _error_envelope(exc.read() if exc.fp is not None else b"")
    if failure is not None:
        return _from_envelope(failure, hint)

    # No envelope: either the backend has not been upgraded yet, or a gateway
    # sitting in the middle spat out a page of HTML of its own. Neither is
    # something a miner can fix, so give a conservative retryable derived from
    # the status code.
    return BackendError(
        f"{url} returned HTTP {exc.code} with no error envelope in the body{hint}",
        retryable=exc.code >= 500 or exc.code == 429,
    )


def _error_envelope(raw: bytes) -> ErrorEnvelope | None:
    """Recognize an error envelope; return None if it is not one (including an
    empty body or non-JSON).

    `ValidationError` is a subclass of `ValueError`, so there is no need to
    import pydantic here -- this repo's dependency on pydantic goes entirely
    through the single path of `openroboto-protocol`.
    """
    try:
        return ErrorEnvelope.model_validate_json(raw)
    except ValueError:
        return None


def _from_envelope(envelope: ErrorEnvelope, hint: str = "") -> BackendError:
    """Copy the fields out of the envelope as they are; reinvent none of
    them."""
    return BackendError(
        envelope.error.message + hint,
        code=envelope.error.code,
        retryable=envelope.error.retryable,
        request_id=envelope.meta.request_id,
    )


def _parse(model: type[_Model], raw: bytes, path: str) -> _Model:
    """Parse a success response using the shape declared by the protocol
    package.

    Failing to parse **is not the miner's fault, and they should not be thrown
    a page of stack trace either**: it means the two sides have installed
    different versions of `openroboto-protocol`, and either side could be the
    outdated one.
    """
    try:
        return model.model_validate_json(raw)
    except ValueError as exc:
        # pydantic's documentation links are pure noise to a miner, so they
        # are not passed on here.
        complaints = [
            line for line in str(exc).splitlines() if "errors.pydantic.dev" not in line
        ]
        detail = "\n  ".join(complaints[:MAX_MISMATCH_LINES])
        raise BackendError(
            f"The response from {path} does not match the shape this version of"
            f" the CLI knows:\n  {detail}\n"
            "  → Run `pip install -U openroboto` first to get the latest; if you"
            " are already on the latest and still see this, the backend has not"
            " caught up yet -- send us the lines above"
        ) from exc


def _decode(raw: bytes, path: str) -> Any:
    """A success response must be JSON. If it is not, a gateway or proxy most
    likely got in the way, so it is retryable."""
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise BackendError(
            f"{path} did not return JSON: {exc}", retryable=True
        ) from exc
