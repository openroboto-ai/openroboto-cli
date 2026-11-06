"""HF cache links are materialized without weakening local package paths."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

import pytest

from openroboto import task_package as tp
from tests.test_task_package import axis_package

REPO = "author/tasks"
COMMIT = "a" * 40


def snapshot(tmp_path: Path) -> tuple[Path, Path]:
    source = axis_package(tmp_path / "original")
    (source / ".gitattributes").write_text("*.bin filter=lfs\n")
    (source / "extra.txt").write_text("ordinary repository content\n")
    cache = tmp_path / "models--author--tasks"
    blobs = cache / "blobs"
    blobs.mkdir(parents=True)
    root = cache / "snapshots" / COMMIT
    root.mkdir(parents=True)
    for index, original in enumerate(source.rglob("*")):
        if not original.is_file():
            continue
        blob = blobs / f"{index:040x}"
        blob.write_bytes(original.read_bytes())
        link = root / original.relative_to(source)
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(os.path.relpath(blob, link.parent))
    return source, root


def download_from(monkeypatch: pytest.MonkeyPatch, root: Path) -> dict[str, Any]:
    import huggingface_hub

    recorded: dict[str, Any] = {}

    def download(**kwargs: Any) -> str:
        recorded.update(kwargs)
        return str(root)

    monkeypatch.setattr(huggingface_hub, "snapshot_download", download)
    return recorded


def test_hf_blob_files_materialize_and_validate_without_added_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original, root = snapshot(tmp_path)
    recorded = download_from(monkeypatch, root)
    fetched = tp.fetch_package(REPO, COMMIT, cache_dir=tmp_path)

    assert fetched != root
    assert not any(path.is_symlink() for path in fetched.rglob("*"))
    expected = {
        path.relative_to(original): path.read_bytes()
        for path in original.rglob("*")
        if path.is_file()
    }
    actual = {
        path.relative_to(fetched): path.read_bytes()
        for path in fetched.rglob("*")
        if path.is_file()
    }
    assert actual == expected
    assert Path(".gitattributes") in actual
    assert not (fetched / ".cache").exists()
    assert not tp.validate_package(tp.load_package(fetched))
    assert recorded["revision"] == COMMIT
    assert recorded["repo_id"] == REPO
    assert "local_dir" not in recorded


@pytest.mark.parametrize("kind", ["outside", "directory", "dangling", "loop"])
def test_snapshot_symlinks_cannot_escape_or_walk_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    _, root = snapshot(tmp_path)
    link = root / "reference.json"
    link.unlink()
    if kind == "outside":
        target = tmp_path / "outside.json"
        target.write_text("{}")
    elif kind == "directory":
        target = root.parent.parent / "blobs"
    elif kind == "dangling":
        target = root.parent.parent / "blobs" / "missing"
    else:
        target = link
    link.symlink_to(target)
    download_from(monkeypatch, root)
    with pytest.raises(tp.PackageFetchError):
        tp.fetch_package(REPO, COMMIT, cache_dir=tmp_path)


def test_arbitrary_snapshot_parent_does_not_authorize_file_links(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = axis_package(tmp_path / "ordinary-directory")
    target = tmp_path / "host-file"
    target.write_text("{}")
    (root / "reference.json").unlink()
    (root / "reference.json").symlink_to(target)
    download_from(monkeypatch, root)
    with pytest.raises(tp.PackageFetchError, match="unsafe HF snapshot symlink"):
        tp.fetch_package(REPO, COMMIT, cache_dir=tmp_path)


def test_hf_shared_blob_second_link_is_materialized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, root = snapshot(tmp_path)
    entry = root / "reference.json"
    blob = entry.resolve()
    shared = tmp_path / "blobs" / "ab" / ("b" * 64)
    shared.parent.mkdir(parents=True)
    expected = blob.read_bytes()
    shared.write_bytes(expected)
    blob.unlink()
    blob.symlink_to(os.path.relpath(shared, blob.parent))
    download_from(monkeypatch, root)
    fetched = tp.fetch_package(REPO, COMMIT, cache_dir=tmp_path)
    assert (fetched / "reference.json").read_bytes() == expected
    assert not any(path.is_symlink() for path in fetched.rglob("*"))
    assert not tp.validate_package(tp.load_package(fetched))


def test_second_blob_link_cannot_authorize_an_arbitrary_host_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, root = snapshot(tmp_path)
    blob = (root / "reference.json").resolve()
    outside = tmp_path / "not-the-shared-store.json"
    outside.write_bytes(blob.read_bytes())
    blob.unlink()
    blob.symlink_to(outside)
    download_from(monkeypatch, root)
    with pytest.raises(tp.PackageFetchError, match="unsafe HF snapshot symlink"):
        tp.fetch_package(REPO, COMMIT, cache_dir=tmp_path)


def test_shared_blob_store_directory_link_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, root = snapshot(tmp_path)
    blob = (root / "reference.json").resolve()
    outside = tmp_path / "host-directory"
    outside.mkdir()
    shared = outside / "file"
    shared.write_bytes(blob.read_bytes())
    (tmp_path / "blobs").symlink_to(outside, target_is_directory=True)
    blob.unlink()
    blob.symlink_to(tmp_path / "blobs" / "file")
    download_from(monkeypatch, root)
    with pytest.raises(tp.PackageFetchError, match="unsafe HF snapshot symlink"):
        tp.fetch_package(REPO, COMMIT, cache_dir=tmp_path)


def test_snapshot_root_directory_link_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, root = snapshot(tmp_path)
    alias = tmp_path / "snapshot-alias"
    alias.symlink_to(root, target_is_directory=True)
    download_from(monkeypatch, alias)
    with pytest.raises(tp.PackageFetchError, match="root must not be a symlink"):
        tp.fetch_package(REPO, COMMIT, cache_dir=tmp_path)


def test_local_package_file_escape_remains_rejected(tmp_path: Path) -> None:
    root = axis_package(tmp_path / "local-package")
    target = tmp_path / "outside-reference.json"
    shutil.move(root / "reference.json", target)
    (root / "reference.json").symlink_to(target)
    package = tp.load_package(root)
    with pytest.raises(tp.TaskPackageError, match="escapes"):
        package.path("reference.json")
    assert "axis.reference_escapes" in {
        issue.code for issue in tp.validate_package(package)
    }


def test_materialization_keeps_malicious_manifest_paths_invalid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    root = axis_package(tmp_path / "repository")
    task_path = root / "task.json"
    task = json.loads(task_path.read_text())
    task["reference_solution"] = "../outside-reference.json"
    task_path.write_text(json.dumps(task))
    download_from(monkeypatch, root)
    fetched = tp.fetch_package(REPO, COMMIT, cache_dir=tmp_path)
    assert "axis.reference_escapes" in {
        issue.code for issue in tp.validate_package(tp.load_package(fetched))
    }


def test_failed_copy_does_not_leave_a_materialized_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, root = snapshot(tmp_path)
    download_from(monkeypatch, root)

    def fail_copy(*args: Any, **kwargs: Any) -> None:
        raise OSError("copy failed")

    monkeypatch.setattr(tp.shutil, "copytree", fail_copy)
    cache = tmp_path
    with pytest.raises(tp.PackageFetchError, match="could not materialize"):
        tp.fetch_package(REPO, COMMIT, cache_dir=cache)
    assert not list(cache.glob("openroboto-task-files-*"))
