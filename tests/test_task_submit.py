"""The pre-payment gate for task-authoring seasons.

A task season's fee buys a verdict on a **task package**; the model layout gate
and the weights fingerprint do not apply. These tests pin the replacement gate:
it judges the repository at the commit that will go on chain and refuses before
any money moves.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from openroboto_protocol.commitment import Track
from openroboto_protocol.schemas import Competition

from openroboto import backend_api
from openroboto.commands import submit as submit_command
from openroboto.competition import PrecheckFailed, Snapshot, judge
from openroboto.config import Settings
from openroboto.task_package import (
    PackageFetchError,
    digest_available,
    package_digest,
)
from tests.test_task_package import axis_package

COMMIT = "a" * 40

pytestmark = pytest.mark.skipif(
    not digest_available(),
    reason="needs the OPE-211 candidate protocol wheel 0.13.0 (not on PyPI)",
)


def _settings() -> Settings:
    return Settings.from_mapping({"huggingface": {"token": ""}})


def _state() -> dict[str, Any]:
    return {
        "hf_repo_id": "someone/task-package",
        "hf_url": "https://huggingface.co/someone/task-package",
        "hf_commit": COMMIT,
    }


def _live(source: str | None = "axis") -> Any:
    params = {} if source is None else {"task": {"source": source}}
    return SimpleNamespace(track="task", label="task/1", params=params)


def test_a_valid_axis_package_passes_and_records_the_digest(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    package = axis_package(tmp_path / "pkg")
    monkeypatch.setattr(submit_command, "fetch_package", lambda *a, **k: package)

    state = _state()
    assert submit_command.task_package_is_payable(_settings(), state, _live()) is True
    assert state["package_digest"] == package_digest(package)
    assert state["package_source"] == "axis"
    assert state["package_task_id"] == "9001"
    assert "package_digest" in capsys.readouterr().out


def test_the_gate_digests_the_nested_package_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The validator digests the package directory, not the repository root."""
    package = axis_package(tmp_path / "repo" / "my-task")
    monkeypatch.setattr(
        submit_command, "fetch_package", lambda *a, **k: tmp_path / "repo"
    )

    state = _state()
    assert submit_command.task_package_is_payable(_settings(), state, _live()) is True
    assert state["package_digest"] == package_digest(package)
    capsys.readouterr()


def test_a_package_for_another_source_is_refused_before_payment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    package = axis_package(tmp_path / "pkg")
    monkeypatch.setattr(submit_command, "fetch_package", lambda *a, **k: package)

    assert (
        submit_command.task_package_is_payable(
            _settings(), _state(), _live(source="robodojo")
        )
        is False
    )
    assert "declares `axis`" in capsys.readouterr().err


def test_a_season_without_a_task_source_is_refused_before_payment(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert (
        submit_command.task_package_is_payable(
            _settings(), _state(), _live(source=None)
        )
        is False
    )
    assert "params.task.source" in capsys.readouterr().err


def test_an_invalid_package_is_refused_before_payment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    package = axis_package(tmp_path / "pkg", drift=True)
    monkeypatch.setattr(submit_command, "fetch_package", lambda *a, **k: package)

    assert (
        submit_command.task_package_is_payable(_settings(), _state(), _live()) is False
    )
    assert "axis.payload_mismatch" in capsys.readouterr().err


def test_an_unreadable_repository_is_not_a_verdict(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _explode(*args: Any, **kwargs: Any) -> None:
        raise PackageFetchError("HF said no")

    monkeypatch.setattr(submit_command, "fetch_package", _explode)
    assert (
        submit_command.task_package_is_payable(_settings(), _state(), _live()) is False
    )
    message = capsys.readouterr().err
    assert "not a verdict on your package" in message


def test_the_upload_must_be_byte_identical_to_the_local_package(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    package = axis_package(tmp_path / "pkg")
    state = {"package_digest": package_digest(package)}
    assert submit_command.task_package_upload_is_unchanged(state, str(package)) is True
    assert state["package_local_digest"] == state["package_digest"]
    assert "upload unchanged" in capsys.readouterr().out

    (package / "run_info.json").write_text("{}", encoding="utf-8")
    assert submit_command.task_package_upload_is_unchanged(state, str(package)) is False
    assert "not byte-identical" in capsys.readouterr().err


def test_a_missing_local_package_does_not_invent_a_verdict(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state = {"package_digest": "a" * 64}
    assert (
        submit_command.task_package_upload_is_unchanged(state, str(tmp_path / "gone"))
        is True
    )
    assert "repository digest stays authoritative" in capsys.readouterr().out


def test_a_task_season_must_be_collected_by_burn() -> None:
    """The backend files and verifies a burn. A transfer season would take the
    fee to a coldkey the backend never checks and never admit the task."""
    fee = {"kind": "transfer", "amount_tao": 0.1, "coldkey": "5" + "A" * 47}
    snapshot = Snapshot(
        {"id": 77, "track": "task", "seq": 1, "label": "Task 1", "params": {"fee": fee}}
    )
    live = SimpleNamespace(
        id=77,
        track="task",
        seq=1,
        label="Task 1",
        status="active",
        submit_opens_at=None,
        submit_closes_at=None,
        params={"fee": fee, "task": {"source": "axis"}},
    )
    with pytest.raises(PrecheckFailed, match="task-authoring"):
        judge(snapshot, [cast(Competition, live)], datetime.now(UTC))


# ── the competition list ────────────────────────────────────────────────────


def test_asking_for_task_seasons_needs_a_protocol_that_knows_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pinned PyPI release predates `Track.TASK`; asking must say so."""
    if getattr(Track, "TASK", None) is not None:
        pytest.skip("the installed protocol already knows Track.TASK")

    with pytest.raises(backend_api.BackendError, match=r"Track\.TASK"):
        backend_api.fetch_competitions("https://api.example", include_task=True)


def test_include_task_is_sent_as_a_query_parameter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    def _get(base_url: str, path: str, query: dict[str, Any]) -> Any:
        seen.update(query)
        raise backend_api.BackendError("stop after the request is built")

    monkeypatch.setattr(backend_api, "_get", _get)
    monkeypatch.setattr(backend_api, "Track", SimpleNamespace(TASK=object()))

    with pytest.raises(backend_api.BackendError):
        backend_api.fetch_competitions("https://api.example", include_task=True)
    assert seen["include_task"] == "true"
