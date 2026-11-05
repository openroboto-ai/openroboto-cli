"""`openroboto task check`: the command a miner runs before paying.

The JSON form is what goes into an evidence record, so its fields are asserted
by name; the human form is what a miner reads, so a digest mismatch must be a
non-zero exit even though the files themselves parsed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest

from openroboto import task_package as tp
from openroboto.cli import main
from tests.test_task_package import axis_package, robodojo_package


def _json_output(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    captured = capsys.readouterr()
    return cast(dict[str, Any], json.loads(captured.out))


def test_json_output_has_the_per_file_digests_and_the_package_digest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    package = axis_package(tmp_path / "pkg")
    assert main(["task", "check", str(package), "--json"]) == 0
    summary = _json_output(capsys)

    assert summary["ok"] is True
    assert summary["source"] == "axis"
    assert summary["task_id"] == "9001"
    assert summary["package_digest"] == tp.package_digest(package)
    assert summary["digest_source"] in (
        "openroboto-protocol",
        "cli-mirror(transitional)",
    )
    assert [item["path"] for item in summary["files"]] == [
        "benchmark.json",
        "payloads/9001-official-00.json",
        "randomization.json",
        "reference.json",
        "task.json",
    ]
    assert all(len(item["sha256"]) == 64 for item in summary["files"])


def test_expect_digest_matches_the_same_value(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    package = axis_package(tmp_path / "pkg")
    digest = tp.package_digest(package)
    assert (
        main(
            [
                "task",
                "check",
                str(package),
                "--json",
                "--expect-digest",
                digest,
            ]
        )
        == 0
    )
    assert _json_output(capsys)["digest_matches"] is True


def test_expect_digest_mismatch_exits_non_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    package = axis_package(tmp_path / "pkg")
    assert (
        main(
            [
                "task",
                "check",
                str(package),
                "--json",
                "--expect-digest",
                "0" * 64,
            ]
        )
        == 1
    )
    summary = _json_output(capsys)
    assert summary["digest_matches"] is False
    assert summary["ok"] is False


def test_an_invalid_package_exits_non_zero_but_still_prints_the_digest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    package = axis_package(tmp_path / "pkg", drift=True)
    assert main(["task", "check", str(package), "--json"]) == 1
    summary = _json_output(capsys)
    assert summary["package_digest"] == tp.package_digest(package)
    assert any(issue["code"] == "axis.payload_mismatch" for issue in summary["issues"])


def test_repo_and_commit_must_be_given_together(tmp_path: Path) -> None:
    assert main(["task", "check", str(tmp_path), "--repo", "someone/tasks"]) == 1


def test_a_nested_package_is_digested_over_its_own_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The validator discovers packages at the root or one/two levels below and
    digests the package directory. A parent directory's digest would not match."""
    package = axis_package(tmp_path / "repo" / "my-task")
    assert main(["task", "check", str(tmp_path / "repo"), "--json"]) == 0
    summary = _json_output(capsys)
    assert summary["package_digest"] == tp.package_digest(package)
    assert summary["package_dir"] == str(package)


def test_two_packages_need_a_selector(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    axis_package(tmp_path / "repo" / "one")
    axis_package(tmp_path / "repo" / "two")
    assert main(["task", "check", str(tmp_path / "repo"), "--json"]) == 1
    assert "more than one task package" in capsys.readouterr().err


def test_run_info_in_a_task_package_is_a_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The uploader must not add it, but a leftover still counts in the digest."""
    package = axis_package(tmp_path / "pkg")
    (package / "run_info.json").write_text("{}", encoding="utf-8")
    assert main(["task", "check", str(package), "--json"]) == 0
    summary = json.loads(capsys.readouterr().out)
    warnings = [item for item in summary["issues"] if item["severity"] == "warning"]
    assert [item["code"] for item in warnings] == ["package.run_info_present"]
    # It is part of the digest on both sides, so it is not an error.
    assert summary["package_digest"] == tp.package_digest(package)


def test_package_option_picks_one_of_several(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    axis_package(tmp_path / "repo" / "one")
    two = axis_package(tmp_path / "repo" / "two")
    assert (
        main(
            [
                "task",
                "check",
                str(tmp_path / "repo"),
                "--package",
                "two",
                "--json",
            ]
        )
        == 0
    )
    summary = _json_output(capsys)
    assert summary["package_digest"] == tp.package_digest(two)


def test_package_option_may_not_escape_the_repository(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    axis_package(tmp_path / "repo" / "one")
    outer = axis_package(tmp_path / "outside")
    assert (
        main(
            [
                "task",
                "check",
                str(tmp_path / "repo"),
                "--package",
                "../outside",
                "--json",
            ]
        )
        == 1
    )
    assert "escapes" in capsys.readouterr().err
    assert outer.is_dir()


def test_source_filter_picks_the_season_source(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    axis_package(tmp_path / "repo" / "axis-task")
    robodojo = robodojo_package(tmp_path / "repo" / "robodojo-task")
    assert (
        main(
            [
                "task",
                "check",
                str(tmp_path / "repo"),
                "--source",
                "robodojo",
                "--json",
            ]
        )
        == 0
    )
    summary = _json_output(capsys)
    assert summary["source"] == "robodojo"
    assert summary["package_digest"] == tp.package_digest(robodojo)


def test_a_missing_directory_is_not_a_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["task", "check", str(tmp_path / "nope"), "--json"]) == 1
    summary = _json_output(capsys)
    assert summary["ok"] is False
    assert summary["issues"][0]["code"] == "package.directory_missing"
