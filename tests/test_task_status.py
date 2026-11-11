"""`openroboto status` and the read-only backend calls behind it.

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
from openroboto import task_package as tp
from openroboto.cli import main
from openroboto.commands import task as task_command
from tests.test_task_package import axis_package

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


def test_task_check_refuses_without_the_shared_digest(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On a pre-0.13.0 pin there is no local mirror to fall back to."""
    # Simulated rather than skipped: the pinned release does provide the module,
    # and the refusal path still has to be exercised.
    monkeypatch.setattr(tp, "_shared_package_files", None)
    monkeypatch.setattr(tp, "_shared_digest_of_files", None)
    monkeypatch.setattr(tp, "_shared_package_digest", None)
    package = axis_package(tmp_path / "pkg")
    assert (
        main(
            [
                "check",
                str(package),
                "--static-only",
                "--config",
                str(_config(tmp_path)),
                "--json",
            ]
        )
        == 1
    )
    summary = json.loads(capsys.readouterr().out)
    assert summary["digest_source"] == "unavailable(protocol release 0.13.0 needed)"
    assert summary["issues"][0]["code"] == "package.digest_unavailable"


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
        "competition: {id: 1, seq: 1, track: task}\n"
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
        main(["status", "--config", str(_config(tmp_path)), "--probe", "--json"]) == 0
    )
    output = json.loads(capsys.readouterr().out)
    assert output["readiness"]["ready"] is True
    assert output["liveness"]["netuid"] == 313


def test_backend_url_overrides_the_config_without_rewriting_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    seen: list[str] = []

    def _live(url: str) -> backend_api.Liveness:
        seen.append(url)
        return backend_api.Liveness(status="ok", competition=7, netuid=313)

    def _ready(url: str) -> backend_api.Readiness:
        seen.append(url)
        return backend_api.Readiness(
            ready=True,
            database=backend_api.ReadinessCheck(ok=True),
            migration=backend_api.ReadinessCheck(ok=True),
        )

    monkeypatch.setattr(task_command, "fetch_liveness", _live)
    monkeypatch.setattr(task_command, "fetch_readiness", _ready)

    config = _config(tmp_path)
    assert (
        main(
            [
                "status",
                "--probe",
                "--config",
                str(config),
                "--backend-url",
                "http://127.0.0.1:9999",
                "--json",
            ]
        )
        == 0
    )
    assert seen == ["http://127.0.0.1:9999", "http://127.0.0.1:9999"]
    assert "http://127.0.0.1:8091" in config.read_text(encoding="utf-8")
    capsys.readouterr()


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
        main(["status", "--config", str(_config(tmp_path)), "--probe", "--json"]) == 1
    )
    output = json.loads(capsys.readouterr().out)
    assert output["readiness"]["ready"] is False


@pytest.mark.parametrize(
    "reward",
    [
        {
            "discrimination": 0.0,
            "hardness": 0.8,
            "effective_hardness": 0.8,
            "score": 0.8,
            "settlement_eligible": True,
            "hardness_proof_hash": "e" * 64,
        },
        {
            "discrimination": 0.0,
            "hardness": 0.8,
            "effective_hardness": 0.0,
            "score": 0.0,
            "settlement_eligible": False,
            "hardness_proof_hash": "e" * 64,
        },
        {},
    ],
    ids=["verified-hard-task", "classified-proof-unavailable", "old-api"],
)
def test_status_preserves_backend_reward_fields_in_json_and_text(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    reward: dict[str, Any],
) -> None:
    """Exercise response parsing and both command outputs without model calls."""
    monkeypatch.setattr(task_command, "open_wallet", lambda settings: _FakeWallet())
    monkeypatch.setattr(
        task_command,
        "fetch_task_access_message",
        lambda base_url, hotkey: f"sign this for {hotkey}",
    )
    _serving(monkeypatch, _envelope([_task(**reward)]))
    command = ["status", "--config", str(_config(tmp_path))]

    assert main([*command, "--json"]) == 0
    parsed = json.loads(capsys.readouterr().out)["tasks"][0]
    for field in (
        "discrimination",
        "hardness",
        "effective_hardness",
        "score",
        "settlement_eligible",
        "hardness_proof_hash",
    ):
        assert parsed[field] == reward.get(field)

    assert main(command) == 0
    printed = capsys.readouterr().out
    assert "task_abc  admitted  axis/grasp-lift" in printed
    assert "someone/axis-task-22@" + "a" * 40 in printed
    if not reward:
        assert "D=unknown  H(current)=unknown  S=unknown  eligible=unknown" in printed
        assert "H(classified): unknown" in printed
        assert "hard proof: (not reported)" in printed
    elif reward["settlement_eligible"]:
        assert "D=0  H(current)=0.8  S=0.8  eligible=yes" in printed
        assert "H(classified): 0.8" in printed
        assert "hard proof: " + "e" * 64 in printed
    else:
        assert "D=0  H(current)=0  S=0  eligible=no" in printed
        assert "H(classified): 0.8" in printed
        assert "hard proof: " + "e" * 64 in printed
