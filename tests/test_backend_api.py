"""后端只读客户端：信封解析与错误翻译。不打真实网络。

这个文件守的三件事，每一件都直接影响矿工要不要再烧一笔 TAO：

1. **成功走 `data`，失败走 `error`**，两者不会被混起来；
2. 报错时 `error.code` / `error.retryable` / `meta.request_id` 一个都不能丢
   —— 丢了矿工就只剩「失败了」三个字，只能来问我们；
3. `/api/weights` 解不出来 = 全网排放停摆，所以两种形状都得认。
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
    """替换掉 urlopen，记下发出去的请求，回一份固定响应。"""
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
    """一个带响应体的 HTTPError —— 错误信封就在这个 body 里。"""
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
        "round_num": 3,
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
        "round_num": 3,
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


# ─── 请求拼装 ────────────────────────────────────────────────


def test_empty_parameters_are_not_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _capture(monkeypatch, _list_envelope([]))
    backend_api.fetch_submissions("https://api.example/", hotkey="", limit=5)
    url = seen[0].full_url
    assert url.startswith("https://api.example/api/v1/submissions/history?")
    assert "hotkey" not in url
    assert "limit=5" in url


def test_every_request_asks_for_the_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    """🔴 每个请求都必须点名要信封，否则后端给的是裸 JSON。

    后端的默认形状是裸 JSON（迁移期为了不打断评测 worker）。漏掉这个头的表现
    **不是报错**：`_error_envelope()` 永远返回 None、`data` 永远取不到，
    本文件里所有解信封的用例照样绿（它们喂的是构造好的信封字节，
    根本不经过真实的 HTTP 头）—— 只有这一条盯着请求本身。

    带不带 key 的两条路径分开走（`_get` 里是两个分支），所以两条都验。
    """
    for kwargs in ({"hotkey": "5X", "limit": 3}, {"hotkey": "", "limit": 5}):
        seen = _capture(monkeypatch, _list_envelope([]))
        backend_api.fetch_submissions("https://api.example", **kwargs)
        accept = seen[0].get_header("Accept")
        assert accept is not None, "请求没带 Accept —— 拿到的会是裸 JSON"
        assert "application/vnd.openroboto.envelope+json" in accept


def test_rejections_need_no_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """对矿工白纸黑字承诺过 No API key required，挂上 key 就是毁约。"""
    seen = _capture(monkeypatch, _list_envelope([_rejection()]))
    page = backend_api.fetch_rejections("https://api.example", hotkey="5X", limit=3)
    assert "/api/v1/scan-rejections?" in seen[0].full_url
    assert seen[0].get_header("X-api-key") is None
    assert page.data[0].reject_reason == "burn_tx_too_old"


# ─── 成功：data / meta.page ──────────────────────────────────


def test_rows_come_from_data_not_from_a_custom_wrapper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """旧形状是 `{success, submissions, total, …}`，现在业务字段只在 `data` 里。"""
    _capture(monkeypatch, _list_envelope([_submission(round_num=9)]))
    page = backend_api.fetch_submissions("https://api.example", hotkey="5X")
    assert [row.round_num for row in page.data] == [9]


def test_paging_comes_from_meta_not_from_data(monkeypatch: pytest.MonkeyPatch) -> None:
    """`has_more` 由后端算好；调用方不再自己拿 offset+len<total 推一遍。"""
    _capture(monkeypatch, _list_envelope([_submission()], total=42, has_more=True))
    page = backend_api.fetch_submissions("https://api.example", hotkey="5X")
    assert page.meta.page.has_more is True
    assert page.meta.page.total == 42


def test_legacy_status_column_is_not_reachable(monkeypatch: pytest.MonkeyPatch) -> None:
    """旧 `status` 列和 `eval_status` 有 52 行不一致，模型里根本没有它。"""
    _capture(
        monkeypatch,
        _list_envelope([_submission(eval_status="rejected", status="done")]),
    )
    page = backend_api.fetch_submissions("https://api.example", hotkey="5X")
    assert page.data[0].eval_status == "rejected"
    assert not hasattr(page.data[0], "status")


# ─── 失败：error.code / retryable / request_id ────────────────


def test_error_envelope_keeps_code_retryable_and_request_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fail_with(monkeypatch, _http_error(400, _error_envelope("BURN_TX_TOO_OLD", False)))
    with pytest.raises(backend_api.BackendError) as excinfo:
        backend_api.fetch_submissions("https://api.example")

    error = excinfo.value
    assert error.code == "BURN_TX_TOO_OLD"
    assert error.retryable is False
    assert error.request_id == REQUEST_ID


def test_non_retryable_error_tells_the_miner_to_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """这一行就是「别再烧 TAO 了」的全部依据，必须打出来。"""
    _fail_with(monkeypatch, _http_error(400, _error_envelope("BURN_TX_TOO_OLD", False)))
    with pytest.raises(backend_api.BackendError) as excinfo:
        backend_api.fetch_submissions("https://api.example")

    rendered = str(excinfo.value)
    assert "烧的那笔交易太旧了" in rendered
    assert "BURN_TX_TOO_OLD" in rendered
    assert "重试不会有不同的结果" in rendered
    assert REQUEST_ID in rendered


def test_retryable_error_says_it_is_worth_retrying(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fail_with(monkeypatch, _http_error(503, _error_envelope("INFRA_ERROR", True)))
    with pytest.raises(backend_api.BackendError) as excinfo:
        backend_api.fetch_submissions("https://api.example")

    assert excinfo.value.retryable is True
    assert "再试一次" in str(excinfo.value)


def test_error_inside_a_200_is_still_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """信封规则：data / error 二选一。200 里带 error 是后端的 bug，也要当错误报。"""
    _capture(monkeypatch, _error_envelope("INFRA_ERROR", True))
    with pytest.raises(backend_api.BackendError) as excinfo:
        backend_api.fetch_submissions("https://api.example")
    assert excinfo.value.code == "INFRA_ERROR"


def test_401_explains_which_key_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """没有信封的 401（旧后端 / 网关）也要说清是哪个 key。"""
    _fail_with(monkeypatch, _http_error(401))
    with pytest.raises(backend_api.BackendError) as excinfo:
        backend_api.fetch_weights("https://api.example")
    assert "public_key" in str(excinfo.value)


def test_http_error_without_envelope_guesses_retryable_from_the_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fail_with(monkeypatch, _http_error(502))
    with pytest.raises(backend_api.BackendError) as excinfo:
        backend_api.fetch_submissions("https://api.example")
    assert excinfo.value.retryable is True


def test_connection_failure_is_retryable(monkeypatch: pytest.MonkeyPatch) -> None:
    _fail_with(monkeypatch, urllib.error.URLError("down"))
    with pytest.raises(backend_api.BackendError) as excinfo:
        backend_api.fetch_submissions("https://api.example")
    assert excinfo.value.retryable is True


def test_shape_mismatch_asks_the_miner_to_upgrade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """后端形状对不上时给一句能照做的话，而不是一页 pydantic 堆栈。"""
    _capture(monkeypatch, {"submissions": [], "total": 0})
    with pytest.raises(backend_api.BackendError) as excinfo:
        backend_api.fetch_submissions("https://api.example")
    assert "pip install -U openroboto" in str(excinfo.value)


# ─── /api/weights —— 钱的出口 ────────────────────────────────


def test_weights_require_the_public_key(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _capture(monkeypatch, {"5A": 0.5, "5B": "不是数字"})
    weights = backend_api.fetch_weights("https://api.example", public_key="pk")
    assert seen[0].get_header("X-api-key") == "pk"
    assert weights == {"5A": 0.5}


def test_weights_accept_the_envelope_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """ADR 02 §8.5 还没裁这个端点要不要套信封 —— 解不出权重就是全网排放停摆。"""
    _capture(monkeypatch, {"data": {"5A": 0.9}, "meta": {"request_id": REQUEST_ID}})
    assert backend_api.fetch_weights("https://api.example", "pk") == {"5A": 0.9}
