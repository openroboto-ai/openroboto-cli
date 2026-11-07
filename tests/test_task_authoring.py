"""Authoring uses the shared options and never silently runs an absent simulator."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from openroboto_protocol.task_contract import execution_profile_ref

from openroboto.cli import main
from openroboto.commands import task_authoring


def test_catalog_is_shared_and_does_not_require_a_runtime(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["task", "catalog", "--json"]) == 0
    profile = json.loads(capsys.readouterr().out)["profiles"][0]
    assert profile["execution_profile"] == execution_profile_ref()
    assert profile["assets"][0]["id"] == "cube-60mm-v1"
    assert main(["task", "catalog"]) == 0
    assert "franka-panda" in capsys.readouterr().out


def test_create_is_offline_exclusive_and_does_not_publish_private_text(
    tmp_path: Path,
) -> None:
    target = tmp_path / "definition.json"
    assert main(["task", "create", str(target)]) == 0
    doc = json.loads(target.read_text())
    assert doc["execution_profile"] == execution_profile_ref()
    assert "public_summary" not in doc
    before = target.read_bytes()
    assert main(["task", "create", str(target)]) == 1
    assert target.read_bytes() == before


def test_create_accepts_only_explicit_valid_public_summary(tmp_path: Path) -> None:
    public = tmp_path / "public.json"
    summary = {
        "title": "Published title",
        "instruction": "Published synopsis",
        "robot": "franka-panda",
        "origin": "original",
        "execution_profile": execution_profile_ref(),
    }
    public.write_text(json.dumps(summary))
    target = tmp_path / "definition.json"
    assert main(["task", "create", str(target), "--public-summary", str(public)]) == 0
    assert json.loads(target.read_text())["public_summary"] == summary
    public.write_text('{"title":"incomplete"}')
    rejected = tmp_path / "rejected.json"
    assert main(["task", "create", str(rejected), "--public-summary", str(public)]) == 1
    assert not rejected.exists()


def capture_runtime(
    monkeypatch: pytest.MonkeyPatch, *, exit_code: int = 0
) -> list[list[str]]:
    calls: list[list[str]] = []

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, exit_code)

    monkeypatch.setattr(task_authoring.subprocess, "run", run)
    return calls


def test_build_uses_explicit_runtime_and_keeps_title_as_one_argument(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    definition = tmp_path / "definition.json"
    assert main(["task", "create", str(definition)]) == 0
    calls = capture_runtime(monkeypatch)
    output = tmp_path / "package"
    assert (
        main(
            [
                "task",
                "build",
                "--definition",
                str(definition),
                "--output",
                str(output),
                "--runtime-python",
                "/selected/python",
                "--task-id",
                "own-task",
                "--title",
                "A title with spaces",
            ]
        )
        == 0
    )
    assert calls[0] == [
        "/selected/python",
        "-m",
        "task_source.authoring",
        "build",
        "--definition",
        str(definition),
        "--output",
        str(output),
        "--task-id",
        "own-task",
        "--title",
        "A title with spaces",
    ]


@pytest.mark.parametrize(
    "change", ["bad-json", "bad-schema", "unknown-field", "bad-profile"]
)
def test_build_rejects_bad_definitions_before_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    definition = tmp_path / "definition.json"
    assert main(["task", "create", str(definition)]) == 0
    d = json.loads(definition.read_text())
    if change == "bad-json":
        definition.write_text("not json")
    else:
        if change == "bad-schema":
            d["schema_version"] = True
        if change == "unknown-field":
            d["gravity"] = [0, 0, 0]
        if change == "bad-profile":
            d["execution_profile"]["digest"] = "0" * 64
        definition.write_text(json.dumps(d))
    calls = capture_runtime(monkeypatch)
    assert (
        main(
            [
                "task",
                "build",
                "--definition",
                str(definition),
                "--output",
                str(tmp_path / "output"),
            ]
        )
        == 1
    )
    assert calls == []


def test_build_does_not_overwrite_existing_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    definition = tmp_path / "definition.json"
    assert main(["task", "create", str(definition)]) == 0
    calls = capture_runtime(monkeypatch)
    assert (
        main(
            [
                "task",
                "build",
                "--definition",
                str(definition),
                "--output",
                str(tmp_path),
            ]
        )
        == 1
    )
    assert calls == []


def test_runtime_unavailable_or_failure_is_not_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = tmp_path / "package"
    package.mkdir()

    def missing(*args: Any, **kwargs: Any) -> Any:
        raise FileNotFoundError("selected runtime is missing")

    monkeypatch.setattr(task_authoring.subprocess, "run", missing)
    assert (
        main(["task", "self-test", str(package), "--runtime-python", "/missing/python"])
        == 1
    )
    capture_runtime(monkeypatch, exit_code=2)
    assert main(["task", "self-test", str(package)]) == 1


def test_record_and_report_files_stay_outside_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = tmp_path / "package"
    package.mkdir()
    script = tmp_path / "solve.py"
    script.write_text("def solve(runtime, task, instance): pass\n")
    calls = capture_runtime(monkeypatch)
    assert (
        main(["task", "record-reference", str(package), "--script", str(script)]) == 0
    )
    assert calls[-1][-4:] == ["record-reference", str(package), "--script", str(script)]
    assert main(["task", "record-reference", str(package), "--example"]) == 0
    assert calls[-1][-3:] == ["record-reference", str(package), "--example"]
    assert (
        main(
            [
                "task",
                "record-reference",
                str(package),
                "--script",
                str(package / "solve.py"),
            ]
        )
        == 1
    )
    assert (
        main(
            [
                "task",
                "record-reference",
                str(package),
                "--script",
                str(tmp_path / "missing.py"),
            ]
        )
        == 1
    )
    assert (
        main(
            [
                "task",
                "record-reference",
                str(tmp_path / "absent"),
                "--script",
                str(script),
            ]
        )
        == 1
    )
    report = tmp_path / "report.json"
    assert main(["task", "self-test", str(package), "--output", str(report)]) == 0
    assert calls[-1][-4:] == ["self-test", str(package), "--output", str(report)]
    assert (
        main(
            [
                "task",
                "self-test",
                str(package),
                "--output",
                str(package / "report.json"),
            ]
        )
        == 1
    )
    report.write_text("preserved")
    assert main(["task", "self-test", str(package), "--output", str(report)]) == 1
    assert report.read_text() == "preserved"
    assert main(["task", "self-test", str(tmp_path / "absent")]) == 1


def test_build_public_summary_is_explicitly_forwarded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    definition = tmp_path / "definition.json"
    assert main(["task", "create", str(definition)]) == 0
    public = tmp_path / "public.json"
    public.write_text(
        json.dumps(
            {
                "title": "A title",
                "instruction": "A public synopsis",
                "robot": "franka-panda",
                "origin": "derived",
                "execution_profile": execution_profile_ref(),
            }
        )
    )
    calls = capture_runtime(monkeypatch)
    assert (
        main(
            [
                "task",
                "build",
                "--definition",
                str(definition),
                "--output",
                str(tmp_path / "output"),
                "--public-summary",
                str(public),
            ]
        )
        == 0
    )
    assert calls[-1][-2:] == ["--public-summary", str(public)]
