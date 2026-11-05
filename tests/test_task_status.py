"""`openroboto task status` and the read-only backend calls behind it.

These are local tests with a fake HTTP layer: no request is sent to 8091 or any
other backend. What they pin is the auth split the task endpoints require --
MINER-level key + hotkey signature, never ADMIN_KEY -- and that the readiness
probe reads the 503 body instead of reporting a bare failure.
"""

from __future__ import annotations

import io
import json
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from openroboto import backend_api
from openroboto.cli import main
from openroboto.commands import task as task_command

HOTKEY = "5" + "M" * 47
MINER_KEY = "miner-level-key"
ADMIN_KEY = "operator-admin-key"


class _FakeResponse(io.BytesIO):
    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()


def _serving(monkeypatch: pytest.MonkeyPatch, payload: Any) -> list[Any]:
    seen: list[Any] = []

    def _urlopen(request: Any, timeout: float) -> _FakeResponse:
        seen.append(request)
        return _FakeResponse(json.dumps(payload).encode())

    monkeypatch.setattr(backend_api, "urlopen", _urlopen)
    return seen


def _envelope(rows: list[Any]) -> dict[str, Any]:
    return {
        "data": rows,
        "meta": {
            "request_id": "req",
            "page": {"total": len(rows), "limit": 20, "offset": 0, "has_more": False},
        },
    }


def _task(**overrides: Any) -> dict[str, Any]:
    row = {
        "task_id": "task_abc",
        "status": "admitted",
        "package_repo": "someone/axis-task-22",
        "package_commit": "a" * 40,
        "package_digest": "b" * 64,
        "source": "axis",
        "skill": "grasp-lift",
        "content_sha256": "c" * 64,
        "visibility": "private",
        "content_state": "retained",
    }
    row.update(overrides)
    return row


# ── client ──────────────────────────────────────────────────────────────────


def test_my_tasks_sends_the_miner_key_and_the_signature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _serving(monkeypatch, _envelope([_task()]))
    result = backend_api.fetch_my_tasks(
        "http://127.0.0.1:8091",
        hotkey=HOTKEY,
        signature="d" * 128,
        miner_key=MINER_KEY,
    )
    request = seen[0]
    assert request.get_header("X-api-key") == MINER_KEY
    assert "hotkey=" in request.full_url
    assert "signature=" in request.full_url
    assert "/api/v1/tasks/mine" in request.full_url
    assert result.data[0].package_digest == "b" * 64


def test_an_admin_key_in_the_config_is_never_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The CLI has no ADMIN_KEY field; an extra key in YAML must not leak out."""
    settings = backend_api.Settings.from_mapping(
        {
            "backend": {
                "url": "http://127.0.0.1:8091",
                "public_key": MINER_KEY,
                "admin_key": ADMIN_KEY,
            }
        }
    )
    assert backend_api.miner_key(settings) == MINER_KEY
    assert ADMIN_KEY not in backend_api.miner_key(settings)


def test_readiness_reads_the_503_body(monkeypatch: pytest.MonkeyPatch) -> None:
    body = {
        "ready": False,
        "database": {"ok": False, "detail": "database unreachable"},
        "migration": {"ok": True},
        "alembic_version": None,
        "expected_head": "0099",
    }

    def _urlopen(request: Any, timeout: float) -> None:
        raise urllib.error.HTTPError(
            request.full_url,
            503,
            "Service Unavailable",
            {},
            io.BytesIO(json.dumps(body).encode()),  # type: ignore[arg-type]
        )

    monkeypatch.setattr(backend_api, "urlopen", _urlopen)
    ready = backend_api.fetch_readiness("http://127.0.0.1:8091")
    assert ready.ready is False
    assert ready.database.ok is False
    assert ready.database.detail == "database unreachable"
    assert ready.expected_head == "0099"


def test_a_503_without_a_readiness_body_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _urlopen(request: Any, timeout: float) -> None:
        raise urllib.error.HTTPError(
            request.full_url,
            503,
            "Service Unavailable",
            {},
            io.BytesIO(b"<html>proxy error</html>"),  # type: ignore[arg-type]
        )

    monkeypatch.setattr(backend_api, "urlopen", _urlopen)
    with pytest.raises(backend_api.BackendError, match="HTTP 503"):
        backend_api.fetch_readiness("http://127.0.0.1:8091")


def test_liveness_reports_the_season_and_netuid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _serving(monkeypatch, {"status": "ok", "competition": 7, "netuid": 313})
    live = backend_api.fetch_liveness("http://127.0.0.1:8091")
    assert (live.status, live.competition, live.netuid) == ("ok", 7, 313)


# ── error translation ───────────────────────────────────────────────────────


def test_unauthorized_points_at_the_miner_key_and_warns_off_admin(capsys) -> None:
    task_command._task_error(backend_api.BackendError("no key", code="UNAUTHORIZED"))
    message = capsys.readouterr().err
    assert "MINER-level" in message
    assert "backend.public_key" in message
    assert "ADMIN_KEY" in message


def test_invalid_signature_points_at_the_access_message(capsys) -> None:
    task_command._task_error(
        backend_api.BackendError("bad sig", code="INVALID_SIGNATURE")
    )
    message = capsys.readouterr().err
    assert "/api/v1/tasks/access-message" in message
    assert "subnet.hotkey" in message


# ── command ─────────────────────────────────────────────────────────────────


def _config(tmp_path: Path) -> Path:
    path = tmp_path / "miner.yaml"
    path.write_text(
        "backend:\n"
        "  url: http://127.0.0.1:8091\n"
        f"  public_key: {MINER_KEY}\n"
        "subnet:\n"
        f"  hotkey_ss58: {HOTKEY}\n",
        encoding="utf-8",
    )
    return path


class _FakeWallet:
    class hotkey:
        ss58_address = HOTKEY

        @staticmethod
        def sign(message: bytes) -> bytes:
            return b"\x01" * 64


def test_status_queries_with_the_miner_key_and_prints_the_package(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    captured: dict[str, Any] = {}

    monkeypatch.setattr(task_command, "open_wallet", lambda settings: _FakeWallet())
    monkeypatch.setattr(
        task_command,
        "fetch_task_access_message",
        lambda base_url, hotkey: f"sign this for {hotkey}",
    )

    def _fetch(base_url: str, **kwargs: Any) -> Any:
        captured.update(kwargs)
        return SimpleNamespace(
            data=[backend_api.PrivateTask(**_task())],
            meta=SimpleNamespace(request_id="req-123"),
        )

    monkeypatch.setattr(task_command, "fetch_my_tasks", _fetch)

    assert (
        main(
            [
                "task",
                "status",
                "--config",
                str(_config(tmp_path)),
                "--run-id",
                "run-abc",
                "--json",
            ]
        )
        == 0
    )
    output = json.loads(capsys.readouterr().out)
    assert output["miner_key_present"] is True
    assert output["hotkey"] == HOTKEY
    assert output["request_id"] == "req-123"
    assert output["run_id"] == "run-abc"
    assert output["tasks"][0]["package_digest"] == "b" * 64
    assert captured["miner_key"] == MINER_KEY
    assert len(captured["signature"]) == 128


def test_probe_json_reports_ready(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    monkeypatch.setattr(
        task_command,
        "fetch_liveness",
        lambda url: backend_api.Liveness(status="ok", competition=7, netuid=313),
    )
    monkeypatch.setattr(
        task_command,
        "fetch_readiness",
        lambda url: backend_api.Readiness(
            ready=True,
            database=backend_api.ReadinessCheck(ok=True),
            migration=backend_api.ReadinessCheck(ok=True),
            alembic_version="0099",
            expected_head="0099",
        ),
    )
    assert (
        main(
            ["task", "status", "--config", str(_config(tmp_path)), "--probe", "--json"]
        )
        == 0
    )
    output = json.loads(capsys.readouterr().out)
    assert output["readiness"]["ready"] is True
    assert output["liveness"]["netuid"] == 313


def test_probe_exits_non_zero_when_not_ready(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    monkeypatch.setattr(
        task_command,
        "fetch_liveness",
        lambda url: backend_api.Liveness(status="ok", competition=7, netuid=313),
    )
    monkeypatch.setattr(
        task_command,
        "fetch_readiness",
        lambda url: backend_api.Readiness(
            ready=False,
            database=backend_api.ReadinessCheck(ok=False, detail="down"),
            migration=backend_api.ReadinessCheck(ok=True),
        ),
    )
    assert (
        main(
            ["task", "status", "--config", str(_config(tmp_path)), "--probe", "--json"]
        )
        == 1
    )
    output = json.loads(capsys.readouterr().out)
    assert output["readiness"]["ready"] is False
