"""Read-only backend client: envelope parsing and error translation. No real network.

The three things this file guards each decide directly whether a miner has to burn
another TAO:

1. **success goes through `data`, failure through `error`**, and the two are never
   mixed up;
2. on an error, `error.code` / `error.retryable` / `meta.request_id` must not be lost
   -- lose them and all the miner has left is "it failed", so they have to come ask us;
3. failing to parse `/api/weights` means emissions stall network-wide, so both shapes
   have to be accepted.
"""

from __future__ import annotations

import io
import json
import urllib.error
from typing import Any

import pytest

from openroboto import backend_api

REQUEST_ID = "01HZZQ7K9M2F"


class _FakeResponse(io.BytesIO):
    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()


def _capture(monkeypatch: pytest.MonkeyPatch, payload: Any) -> list[Any]:
    """Replace urlopen, record the outgoing request, return a fixed response."""
    seen: list[Any] = []

    def _urlopen(request: Any, timeout: float) -> _FakeResponse:
        seen.append(request)
        return _FakeResponse(json.dumps(payload).encode())

    monkeypatch.setattr(backend_api, "urlopen", _urlopen)
    return seen


def _fail_with(monkeypatch: pytest.MonkeyPatch, exc: Exception) -> None:
    def _urlopen(*args: Any, **kwargs: Any) -> None:
        raise exc

    monkeypatch.setattr(backend_api, "urlopen", _urlopen)


def _http_error(code: int, body: Any = None) -> urllib.error.HTTPError:
    """An HTTPError with a body -- the error envelope lives in that body."""
    fp = io.BytesIO(json.dumps(body).encode()) if body is not None else None
    return urllib.error.HTTPError("https://api.example/x", code, "boom", {}, fp)  # type: ignore[arg-type]


def _list_envelope(rows: list[Any], **page: Any) -> dict[str, Any]:
    meta = {"total": len(rows), "limit": 20, "offset": 0, "has_more": False, **page}
    return {"data": rows, "meta": {"request_id": REQUEST_ID, "page": meta}}


def _error_envelope(code: str, retryable: bool) -> dict[str, Any]:
    error = {"code": code, "message": "烧的那笔交易太旧了", "retryable": retryable}
    return {"error": error, "meta": {"request_id": REQUEST_ID}}


def _submission(**overrides: Any) -> dict[str, Any]:
    row = {
        "id": 1,
        "task_id": "task-1",
        "uid": 7,
        "hotkey": "5X",
        "hf_repo_id": "miner/model",
        "hf_commit": "c0ffee",
        "commit_block": 1200,
        "commit_block_timestamp": 1_700_000_000,
        "burn_tx_hash": "0xdead",
        "burn_block": 1180,
        "burn_status": "confirmed",
        "block_hash": "0xbeef",
        "eval_status": "done",
    }
    return {**row, **overrides}


def _rejection(**overrides: Any) -> dict[str, Any]:
    row = {
        "uid": 7,
        "hotkey": "5X",
        "hf_commit": "c0ffee",
        "hf_repo_id": "miner/model",
        "commit_block": 1200,
        "burn_tx_hash": "0xdead",
        "burn_block": 1180,
        "commit_block_timestamp": 1_700_000_000,
        "task_id": "task-1",
        "reject_reason": "burn_tx_too_old",
    }
    return {**row, **overrides}


# ─── request assembly ────────────────────────────────────────


def test_empty_parameters_are_not_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _capture(monkeypatch, _list_envelope([]))
    backend_api.fetch_submissions("https://api.example/", 2, hotkey="", limit=5)
    url = seen[0].full_url
    assert url.startswith("https://api.example/api/v1/submissions/history?")
    assert "hotkey" not in url
    assert "limit=5" in url


def test_every_request_asks_for_the_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    """🔴 Every request must explicitly ask for the envelope, or the backend returns
    bare JSON.

    The backend's default shape is bare JSON (during migration, so the evaluation
    workers are not disrupted). Missing this header does **not** surface as an error:
    `_error_envelope()` would always return None and `data` would never be reachable,
    while every envelope-parsing test in this file stays green (they are fed
    pre-built envelope bytes and never go through a real HTTP header) -- only this test
    watches the request itself.

    With and without a key are separate paths (two branches inside `_get`), so both
    are verified.
    """
    for kwargs in ({"hotkey": "5X", "limit": 3}, {"hotkey": "", "limit": 5}):
        seen = _capture(monkeypatch, _list_envelope([]))
        backend_api.fetch_submissions("https://api.example", 2, **kwargs)
        accept = seen[0].get_header("Accept")
        assert accept is not None, (
            "the request carries no Accept -- what comes back will be bare JSON"
        )
        assert "application/vnd.openroboto.envelope+json" in accept


def test_rejections_need_no_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """We promised miners in writing that no API key is required; attaching a key
    breaks that promise."""
    seen = _capture(monkeypatch, _list_envelope([_rejection()]))
    page = backend_api.fetch_rejections("https://api.example", hotkey="5X", limit=3)
    assert "/api/v1/scan-rejections?" in seen[0].full_url
    assert seen[0].get_header("X-api-key") is None
    assert page.data[0].reject_reason == "burn_tx_too_old"


# ─── success: data / meta.page ───────────────────────────────


def test_rows_come_from_data_not_from_a_custom_wrapper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The old shape was `{success, submissions, total, ...}`; the business fields now
    live only under `data`."""
    # ⚠️ Asserts on `uid` because the season is not in the response model at
    # `SubmissionHistoryItem`. What this pins is that rows are parsed out of
    # `data`; any business field proves it.
    _capture(monkeypatch, _list_envelope([_submission(uid=9)]))
    page = backend_api.fetch_submissions("https://api.example", 2, hotkey="5X")
    assert [row.uid for row in page.data] == [9]


def test_the_competition_filter_goes_out_on_the_wire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The season filter is handed to the backend, not applied to the rows here.

    🔴 The season is not in either response model, so filtering after the rows
    arrive is not possible at all -- and `?competition=` is **required** by
    `/submissions/history`, which answers 422 without it.

    This asserts the parameter really goes out: an implementation that quietly
    dropped it would show a miner every season's rows while he asked for one,
    and nothing would look wrong.
    """
    seen = _capture(monkeypatch, _list_envelope([]))
    backend_api.fetch_submissions("https://api.example", 2, hotkey="5X")
    assert "competition=2" in seen[0].full_url, seen[0].full_url


def test_rejections_are_not_filtered_by_competition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """🔴 Chain-scan rejections happen **before** admission, so those rows carry
    no season at all -- the endpoint can only filter on the ordinal the miner
    put in their own payload.

    A miner reads this list precisely because a submission did not arrive where
    they expected, so filtering it by a number that payload may itself have got
    wrong hides the row they came for.
    """
    seen = _capture(monkeypatch, _list_envelope([]))
    backend_api.fetch_rejections("https://api.example", hotkey="5X")
    assert "competition" not in seen[0].full_url
    assert "claimed_round" not in seen[0].full_url


def test_paging_comes_from_meta_not_from_data(monkeypatch: pytest.MonkeyPatch) -> None:
    """`has_more` is computed by the backend; callers no longer derive it themselves
    from offset+len<total."""
    _capture(monkeypatch, _list_envelope([_submission()], total=42, has_more=True))
    page = backend_api.fetch_submissions("https://api.example", 2, hotkey="5X")
    assert page.meta.page.has_more is True
    assert page.meta.page.total == 42


def test_legacy_status_column_is_not_reachable(monkeypatch: pytest.MonkeyPatch) -> None:
    """The old `status` column disagreed with `eval_status` on 52 rows; the model does
    not carry it at all."""
    _capture(
        monkeypatch,
        _list_envelope([_submission(eval_status="rejected", status="done")]),
    )
    page = backend_api.fetch_submissions("https://api.example", 2, hotkey="5X")
    assert page.data[0].eval_status == "rejected"
    assert not hasattr(page.data[0], "status")


# ─── failure: error.code / retryable / request_id ────────────


def test_error_envelope_keeps_code_retryable_and_request_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fail_with(monkeypatch, _http_error(400, _error_envelope("BURN_TX_TOO_OLD", False)))
    with pytest.raises(backend_api.BackendError) as excinfo:
        backend_api.fetch_submissions("https://api.example", 2)

    error = excinfo.value
    assert error.code == "BURN_TX_TOO_OLD"
    assert error.retryable is False
    assert error.request_id == REQUEST_ID


def test_non_retryable_error_tells_the_miner_to_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """This one line is the entire basis for "stop burning TAO", so it must be
    printed."""
    _fail_with(monkeypatch, _http_error(400, _error_envelope("BURN_TX_TOO_OLD", False)))
    with pytest.raises(backend_api.BackendError) as excinfo:
        backend_api.fetch_submissions("https://api.example", 2)

    rendered = str(excinfo.value)
    assert "烧的那笔交易太旧了" in rendered
    assert "BURN_TX_TOO_OLD" in rendered
    assert "Retrying will not give a different result" in rendered
    assert REQUEST_ID in rendered


def test_retryable_error_says_it_is_worth_retrying(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fail_with(monkeypatch, _http_error(503, _error_envelope("INFRA_ERROR", True)))
    with pytest.raises(backend_api.BackendError) as excinfo:
        backend_api.fetch_submissions("https://api.example", 2)

    assert excinfo.value.retryable is True
    assert "retrying it as-is" in str(excinfo.value)


def test_error_inside_a_200_is_still_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Envelope rule: data or error, never both. An error inside a 200 is a backend bug
    and must still be reported as an error."""
    _capture(monkeypatch, _error_envelope("INFRA_ERROR", True))
    with pytest.raises(backend_api.BackendError) as excinfo:
        backend_api.fetch_submissions("https://api.example", 2)
    assert excinfo.value.code == "INFRA_ERROR"


def test_401_explains_which_key_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 401 without an envelope (old backend / gateway) must still say which key is
    meant."""
    _fail_with(monkeypatch, _http_error(401))
    with pytest.raises(backend_api.BackendError) as excinfo:
        backend_api.fetch_weights("https://api.example")
    assert "public_key" in str(excinfo.value)


def test_http_error_without_envelope_guesses_retryable_from_the_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fail_with(monkeypatch, _http_error(502))
    with pytest.raises(backend_api.BackendError) as excinfo:
        backend_api.fetch_submissions("https://api.example", 2)
    assert excinfo.value.retryable is True


def test_connection_failure_is_retryable(monkeypatch: pytest.MonkeyPatch) -> None:
    _fail_with(monkeypatch, urllib.error.URLError("down"))
    with pytest.raises(backend_api.BackendError) as excinfo:
        backend_api.fetch_submissions("https://api.example", 2)
    assert excinfo.value.retryable is True


def test_shape_mismatch_asks_the_miner_to_upgrade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the backend shape does not match, give one actionable sentence instead of a
    page of pydantic stack trace."""
    _capture(monkeypatch, {"submissions": [], "total": 0})
    with pytest.raises(backend_api.BackendError) as excinfo:
        backend_api.fetch_submissions("https://api.example", 2)
    assert "pip install -U openroboto" in str(excinfo.value)


# ─── /api/weights -- where the money goes out ────────────────


def test_weights_require_the_public_key(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _capture(monkeypatch, {"5A": 0.5, "5B": "不是数字"})
    weights = backend_api.fetch_weights("https://api.example", public_key="pk")
    assert seen[0].get_header("X-api-key") == "pk"
    assert weights == {"5A": 0.5}


def test_weights_accept_the_envelope_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """ADR 02 section 8.5 has not yet ruled whether this endpoint gets an envelope --
    and failing to parse the weights means emissions stall network-wide."""
    _capture(monkeypatch, {"data": {"5A": 0.9}, "meta": {"request_id": REQUEST_ID}})
    assert backend_api.fetch_weights("https://api.example", "pk") == {"5A": 0.9}


def test_weights_prefers_v1(monkeypatch: pytest.MonkeyPatch) -> None:
    """v1 first. The CLI called two v1 endpoints and one pre-v1 one, and the
    pre-v1 one was the only thing a validator actually needs -- because v1 had
    no weights endpoint at all until 2026-08-22. A validator that can only be
    served by the compatibility layer is a validator that stops working the day
    that layer is switched off, which is the layer's whole stated purpose."""
    seen: list[str] = []

    def fake_get(base_url: str, path: str, **kw: object) -> bytes:
        seen.append(path)
        return b'{"data": {"weights": {"5A": 0.9, "5B": 0.1}}, "meta": {}}'

    monkeypatch.setattr(backend_api, "_get", fake_get)

    assert backend_api.fetch_weights("http://x") == {"5A": 0.9, "5B": 0.1}
    assert seen == ["/api/v1/weights"], "should not have touched the old address"


def test_weights_falls_back_to_the_old_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """🔴 The backend being replaced serves only `/api/weights`, and it is what
    production runs today. A validator upgraded ahead of its backend must keep
    setting weights -- emissions stopping silently is the exact failure this
    function is written to avoid."""
    seen: list[str] = []

    def fake_get(base_url: str, path: str, **kw: object) -> bytes:
        seen.append(path)
        if path == backend_api.WEIGHTS_PATH:
            raise backend_api.BackendError("404")
        return b'{"5A": 0.9, "5B": 0.1}'

    monkeypatch.setattr(backend_api, "_get", fake_get)

    assert backend_api.fetch_weights("http://x") == {"5A": 0.9, "5B": 0.1}
    assert seen == ["/api/v1/weights", "/api/weights"]


def test_weights_unwraps_only_one_level_of_weights_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bare shape has hotkeys at the top level. Unwrapping a `weights` key
    unconditionally would eat a miner whose hotkey happened to be that string --
    it cannot be, hotkeys are ss58, but the check is cheap and the failure mode
    (one miner silently paid nothing) is not visible in any response."""
    monkeypatch.setattr(backend_api, "_get", lambda *a, **k: b'{"5A": 0.9, "5B": 0.1}')

    assert backend_api.fetch_weights("http://x") == {"5A": 0.9, "5B": 0.1}


# ─── competitions ────────────────────────────────────────────


def _competition_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "id": 3,
        "track": "real",
        "seq": 1,
        "label": "xArm 6 第一届",
        "adapter": "real_xarm6",
        "status": "active",
        "submit_closes_at": "2026-09-10T00:00:00Z",
        "base_repo": None,
        "base_revision": None,
        "params": {"fee": {"kind": "transfer", "amount_tao": 2.0, "coldkey": None}},
    }
    return {**row, **overrides}


def test_competitions_come_back_as_the_protocol_declares_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _capture(monkeypatch, _list_envelope([_competition_row()]))

    listed = backend_api.fetch_competitions("https://api.example")

    row = listed.data[0]
    assert (row.track, row.seq, row.adapter) == ("real", 1, "real_xarm6")
    # 🔴 `null` survives as `None`. Filled in with anything here, the CLI's
    # fail-closed gate never fires and the fee leaves for an address nobody
    # holds the key to.
    assert row.params["fee"]["coldkey"] is None
    assert listed.meta.page.total == 1


def test_the_archived_flag_is_the_one_fastapi_declares(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⚠️ `?archived=1` is not a synonym: FastAPI drops query parameters it has
    not declared, so the archived season never comes back **and nothing reports
    an error** -- it just looks like a shorter list."""
    seen = _capture(monkeypatch, _list_envelope([]))
    backend_api.fetch_competitions("https://api.example", include_archived=True)
    assert "include_archived=true" in seen[0].full_url


def test_the_archived_flag_is_absent_when_it_is_not_asked_for(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _capture(monkeypatch, _list_envelope([]))
    backend_api.fetch_competitions("https://api.example")
    assert "include_archived" not in seen[0].full_url


def test_competitions_need_no_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """A miner who has just run `pip install` holds no key, and this is their
    first call. A key attached here fails exactly like a 404 does."""
    seen = _capture(monkeypatch, _list_envelope([]))
    backend_api.fetch_competitions("https://api.example")
    assert seen[0].get_header("X-api-key") is None


def test_a_competition_list_in_the_wrong_shape_is_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It feeds the pre-payment check. Parsing it loosely means comparing a fee
    against `None` and calling that a match."""
    _capture(monkeypatch, _list_envelope([{"id": 3, "track": "real"}]))
    with pytest.raises(backend_api.BackendError, match="does not match the shape"):
        backend_api.fetch_competitions("https://api.example")


# ─── which subnet the backend watches ────────────────────────
#
# `openroboto init` writes this number into `subnet.netuid`, so everything here
# is about one question: is the value in front of us really the backend's answer,
# or something that merely parsed?


def test_the_backend_states_its_own_netuid(monkeypatch: pytest.MonkeyPatch) -> None:
    """`/healthz` is bare JSON on purpose (backend ADR 02 §3.3) -- probes are
    read by orchestrators on fixed field paths, so it carries no envelope."""
    seen = _capture(monkeypatch, {"status": "ok", "competition": 1, "netuid": 313})
    assert backend_api.fetch_netuid("https://api.example") == 313
    assert seen[0].full_url == "https://api.example/healthz"


def test_a_backend_that_does_not_answer_the_probe_is_not_a_netuid_of_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """🔴 Every way of not knowing has to raise.

    A missing key reads as `None`, an old backend returns a page of HTML, a
    misconfigured one could serve `0` -- and each of those, turned into a number
    by a `.get(..., 0)` or an `int()`, is a subnet this miner never chose. The
    fee is burned on whatever comes out of here.
    """
    for payload in ({"status": "ok"}, {"netuid": 0}, {"netuid": "313"}, ["ok"]):
        _capture(monkeypatch, payload)
        with pytest.raises(backend_api.BackendError, match="not a subnet number"):
            backend_api.fetch_netuid("https://api.example")


def test_an_unreachable_probe_says_what_it_was_for(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The miner asked for a workspace, not for a health check; the message has
    to connect the two, and keep the retry advice that came with it."""
    _fail_with(monkeypatch, _http_error(404))
    with pytest.raises(backend_api.BackendError) as excinfo:
        backend_api.fetch_netuid("https://api.example")
    assert "which subnet it watches" in str(excinfo.value)
    assert "Nothing was written" in str(excinfo.value)


# ─── roster ──────────────────────────────────────────────────


def test_the_roster_reads_payment_status_not_burn_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The new endpoints call it by its real name -- a fee can be a transfer.
    The older ones still say `burn_*`; reading the wrong one here yields an
    empty string and a miner told nothing about their payment."""
    row = {
        "hotkey": "5Hb5muCtV2SqiVkZ",
        "uid": 23,
        "hf_repo_id": "miner/model",
        "submitted_at": "2026-08-24T00:00:00Z",
        "payment_status": "paid",
        "hf_access_status": "verified",
        "invalid_reason": None,
        "counts_as_submitted": True,
    }
    seen = _capture(monkeypatch, _list_envelope([row]))

    listed = backend_api.fetch_roster(
        "https://api.example", 3, hotkey="5Hb5muCtV2SqiVkZ"
    )

    assert listed.data[0].payment_status == "paid"
    assert "/api/v1/competitions/3/roster?" in seen[0].full_url
    assert "hotkey=5Hb5muCtV2SqiVkZ" in seen[0].full_url
