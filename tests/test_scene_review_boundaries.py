"""Regression probes for special-file inputs and paginated author registration.

Only local file I/O and simulated HTTP responses are exercised. No simulator,
wallet signing, HF writes, chain requests or model calls are performed.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from openroboto_protocol.schemas import ListEnvelope

from openroboto import scene_package
from openroboto.backend_api import BackendError, PrivateTask
from openroboto.cli import main
from openroboto.commands import task_scene
from openroboto.config import Settings
from tests.test_scene_cli import FIXTURE, progress


@pytest.fixture
def submission(tmp_path):
    root = tmp_path / "input"
    shutil.copytree(FIXTURE, root)
    return root


@pytest.mark.parametrize("command", ["build", "check"])
@pytest.mark.parametrize("kind", ["fifo", "directory", "oversized"])
def test_real_cli_rejects_special_task_json_without_blocking(
    submission, tmp_path, command, kind
):
    entry = submission / "execution/task.json"
    entry.unlink()
    if kind == "fifo":
        os.mkfifo(entry)
    elif kind == "directory":
        entry.mkdir()
    else:
        limit = max(
            p["max_package_bytes"]
            for p in scene_package.profile_catalog()["resources"].values()
        )
        with entry.open("wb") as stream:
            stream.truncate(limit + 1)
    args = ["task", "scene", command]
    if command == "build":
        args += [
            "--execution",
            str(submission / "execution"),
            "--private",
            str(submission / "private"),
            "--output",
            str(tmp_path / "output"),
        ]
    else:
        args.append(str(submission))
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from openroboto.cli import main; raise SystemExit(main())",
            *args,
        ],
        capture_output=True,
        text=True,
        timeout=3,
    )
    assert result.returncode == 1
    assert json.loads(result.stdout)["issues"][0]["code"] == (
        "resource_limit" if kind == "oversized" else "path_unsafe"
    )
    assert not (tmp_path / "output").exists()


def test_json_size_is_rejected_before_open_or_parse(tmp_path, monkeypatch):
    limit = max(
        p["max_package_bytes"]
        for p in scene_package.profile_catalog()["resources"].values()
    )
    entry = tmp_path / "task.json"
    with entry.open("wb") as stream:
        stream.truncate(limit + 1)

    def forbidden(*a, **k):
        raise AssertionError(
            "Oversized JSON must be rejected before opening or parsing"
        )

    monkeypatch.setattr(Path, "open", forbidden)
    monkeypatch.setattr(os, "open", forbidden)
    monkeypatch.setattr(scene_package.json, "loads", forbidden)
    with pytest.raises(scene_package.ScenePackageError) as caught:
        scene_package._json(entry)
    assert caught.value.finding["code"] == "resource_limit"


def test_device_is_rejected_before_open(monkeypatch):
    def forbidden(*a, **k):
        raise AssertionError("A character device must not be opened")

    monkeypatch.setattr(Path, "open", forbidden)
    monkeypatch.setattr(os, "open", forbidden)
    with pytest.raises(scene_package.ScenePackageError) as caught:
        scene_package._json(Path("/dev/null"))
    assert caught.value.finding["code"] == "path_unsafe"


@pytest.mark.parametrize("replacement", ["fifo", "oversized", "symlink"])
def test_descriptor_recheck_and_open_flags_reject_replaced_files(
    tmp_path, monkeypatch, replacement
):
    path = tmp_path / "task.json"
    path.write_bytes(b"{}")
    other = tmp_path / "other.json"
    other.write_bytes(b"{}")
    original_open = os.open

    def replace_then_open(name, flags):
        assert flags & os.O_NONBLOCK and flags & os.O_NOFOLLOW
        path.unlink()
        if replacement == "fifo":
            os.mkfifo(path)
        elif replacement == "symlink":
            path.symlink_to(other)
        else:
            path.write_bytes(b"0123456789")
        return original_open(name, flags)

    monkeypatch.setattr(os, "open", replace_then_open)
    if replacement == "symlink":
        with pytest.raises(OSError):
            scene_package._read_regular(path, limit=4)
    else:
        with pytest.raises(scene_package.ScenePackageError) as caught:
            scene_package._read_regular(path, limit=4)
        assert caught.value.finding["code"] == (
            "path_unsafe" if replacement == "fifo" else "resource_limit"
        )


def test_growth_during_read_remains_bounded(tmp_path, monkeypatch):
    path = tmp_path / "task.json"
    path.write_bytes(b"{}")
    original_fdopen = os.fdopen

    def grow_then_read(fd, *args, **kwargs):
        path.write_bytes(b"0123456789")
        return original_fdopen(fd, *args, **kwargs)

    monkeypatch.setattr(os, "fdopen", grow_then_read)
    with pytest.raises(scene_package.ScenePackageError) as caught:
        scene_package._read_regular(path, limit=4)
    assert caught.value.finding["code"] == "resource_limit"


@pytest.fixture
def registration(submission, monkeypatch):
    settings = Settings(backend_url="https://example.invalid", hotkey_ss58="author")
    monkeypatch.setattr(task_scene.Settings, "load", lambda *a: settings)
    monkeypatch.setattr(
        task_scene,
        "open_wallet",
        lambda *a: SimpleNamespace(hotkey=SimpleNamespace(ss58_address="author")),
    )
    monkeypatch.setattr(task_scene, "miner_key", lambda *a: "MINER-FIXTURE")
    monkeypatch.setattr(
        task_scene, "fetch_task_access_message", lambda *a: "access-fixture"
    )
    signed = []

    def sign(wallet, message):
        signed.append(message)
        return "a" * 128

    monkeypatch.setattr(task_scene, "hotkey_signature", sign)
    downloaded, registered = [], []

    def fetch(repo, commit, **kwargs):
        downloaded.append((repo, commit))
        return submission

    monkeypatch.setattr(task_scene, "fetch_package", fetch)

    def register(*args):
        registered.append(args)
        return progress(submission)

    monkeypatch.setattr(task_scene, "register_scene", register)
    return downloaded, registered, signed


def author_rows(count, target_index=None):
    return [
        PrivateTask(
            task_id="paid-task" if i == target_index else f"newer-{i}",
            status="submitted",
            package_repo=f"author/package-{i}",
            package_commit=f"{i:040x}",
        )
        for i in range(count)
    ]


def page(rows, offset, limit, *, has_more=None):
    data = rows[offset : offset + limit]
    return ListEnvelope[PrivateTask].model_validate(
        {
            "data": [row.model_dump() for row in data],
            "meta": {
                "request_id": "interface-fixture",
                "page": {
                    "total": len(rows),
                    "limit": limit,
                    "offset": offset,
                    "has_more": offset + len(data) < len(rows)
                    if has_more is None
                    else has_more,
                },
            },
        }
    )


@pytest.mark.parametrize("target_index", [100, 249])
def test_register_finds_exact_task_after_first_page(
    submission, registration, monkeypatch, capsys, target_index
):
    rows = author_rows(target_index + 1, target_index)
    calls = []

    def fetch(base, **kwargs):
        assert kwargs["hotkey"] == "author" and kwargs["signature"] == "a" * 128
        assert kwargs["miner_key"] == "MINER-FIXTURE"
        calls.append((kwargs.get("offset", 0), kwargs["limit"]))
        return page(rows, kwargs.get("offset", 0), kwargs["limit"])

    monkeypatch.setattr(task_scene, "fetch_my_tasks", fetch)
    assert (
        main(["task", "scene", "register", "paid-task", "--package", str(submission)])
        == 0
    )
    assert [offset for offset, _ in calls] == list(range(0, target_index + 1, 100))
    downloaded, registered, signed = registration
    assert downloaded == [
        (rows[target_index].package_repo, rows[target_index].package_commit)
    ]
    assert registered[0][1] == "paid-task"
    message = json.loads(signed[-1])
    assert message["package_repo"] == rows[target_index].package_repo
    assert message["package_commit"] == rows[target_index].package_commit
    assert len(capsys.readouterr().out) > 0


@pytest.mark.parametrize("count,empty_tail", [(0, False), (101, False), (100, True)])
def test_registration_exhaustion_has_no_download_or_register(
    submission, registration, monkeypatch, capsys, count, empty_tail
):
    rows = author_rows(count)
    calls = []

    def fetch(base, **kwargs):
        offset = kwargs.get("offset", 0)
        calls.append(offset)
        return page(
            rows, offset, kwargs["limit"], has_more=True if empty_tail else None
        )

    monkeypatch.setattr(task_scene, "fetch_my_tasks", fetch)
    assert (
        main(["task", "scene", "register", "paid-task", "--package", str(submission)])
        == 1
    )
    assert json.loads(capsys.readouterr().out)["issues"][0]["code"] == "task_not_found"
    assert calls == ([0] if count == 0 else [0, 100])
    assert registration[0] == registration[1] == []


def test_lookup_page_cap_is_distinct_and_resumable(
    submission, registration, monkeypatch, capsys
):
    rows = author_rows(201, 200)
    calls = []
    monkeypatch.setattr(task_scene, "REGISTRATION_MAX_PAGES", 2)

    def fetch(base, **kwargs):
        offset = kwargs["offset"]
        calls.append(offset)
        return page(rows, offset, kwargs["limit"])

    monkeypatch.setattr(task_scene, "fetch_my_tasks", fetch)
    args = ["task", "scene", "register", "paid-task", "--package", str(submission)]
    assert main(args) == 1
    finding = json.loads(capsys.readouterr().out)["issues"][0]
    assert finding["code"] == "task_lookup_limit"
    assert "--lookup-offset 200" in finding["message"]
    assert calls == [0, 100]
    assert registration[0] == registration[1] == []
    assert main([*args, "--lookup-offset", "200"]) == 0
    assert calls == [0, 100, 200]
    assert registration[0] == [(rows[200].package_repo, rows[200].package_commit)]
    assert main([*args, "--lookup-offset", "200"]) == 0
    assert registration[1][-1] == registration[1][-2]


def test_negative_lookup_offset_does_not_query_pages(
    submission, registration, monkeypatch, capsys
):
    def forbidden(*a, **k):
        raise AssertionError("Invalid offsets must not request an author page")

    monkeypatch.setattr(task_scene, "fetch_my_tasks", forbidden)
    assert (
        main(
            [
                "task",
                "scene",
                "register",
                "paid-task",
                "--package",
                str(submission),
                "--lookup-offset",
                "-1",
            ]
        )
        == 1
    )
    assert (
        json.loads(capsys.readouterr().out)["issues"][0]["code"]
        == "lookup_offset_invalid"
    )
    assert registration[0] == registration[1] == []


@pytest.mark.parametrize("failure", ["auth", "pagination"])
def test_failed_second_page_preserves_auth_and_has_no_registration(
    submission, registration, monkeypatch, capsys, failure
):
    rows = author_rows(101, 100)
    calls = []

    def fetch(base, **kwargs):
        offset = kwargs["offset"]
        calls.append(offset)
        if offset:
            if failure == "auth":
                raise BackendError("fixture refusal", code="INVALID_SIGNATURE")
            return page(rows, 0, kwargs["limit"])
        return page(rows, offset, kwargs["limit"])

    monkeypatch.setattr(task_scene, "fetch_my_tasks", fetch)
    assert (
        main(["task", "scene", "register", "paid-task", "--package", str(submission)])
        == 1
    )
    result = json.loads(capsys.readouterr().out)
    assert result["code"] == (
        "INVALID_SIGNATURE" if failure == "auth" else "TASK_LOOKUP_PAGE_MISMATCH"
    )
    assert calls == [0, 100]
    assert registration[0] == registration[1] == []


def test_lookup_does_not_register_a_different_private_identity(
    submission, registration, monkeypatch, capsys, tmp_path
):
    rows = author_rows(101, 100)
    monkeypatch.setattr(
        task_scene,
        "fetch_my_tasks",
        lambda base, **k: page(rows, k["offset"], k["limit"]),
    )
    remote = scene_package.build_submission(
        submission / "execution", tmp_path / "remote", private=submission / "private"
    )
    (remote.root / "private/extra.json").write_text("{}")
    changed = scene_package.build_submission(
        remote.root / "execution", tmp_path / "changed", private=remote.root / "private"
    )
    monkeypatch.setattr(task_scene, "fetch_package", lambda *a, **k: changed.root)
    assert (
        main(["task", "scene", "register", "paid-task", "--package", str(submission)])
        == 1
    )
    assert (
        json.loads(capsys.readouterr().out)["issues"][0]["code"] == "submission_drift"
    )
    assert registration[1] == []
