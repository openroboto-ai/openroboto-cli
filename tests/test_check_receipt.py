"""Receipt privacy, package identity and stale-success invalidation."""

import json

import pytest

from openroboto.check_receipt import (
    invalidate_receipt,
    receipt_path,
    write_receipt,
)
from openroboto.cli import main
from openroboto.config import ConfigError, Settings
from openroboto.task_package import package_digest
from tests.task_workspace_fixture import write_task_config
from tests.test_task_package import axis_package


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = write_task_config(tmp_path)
    return Settings.load(str(config)), str(config), tmp_path


def full_result(digest):
    return {
        "ok": True,
        "ready_to_submit": True,
        "package_digest": digest,
        "image_digest": "sha256:" + "1" * 64,
        "checks": [
            {
                "name": "runtime.solvable",
                "passed": True,
                "kind": "runtime",
                "detail": {"private_instruction": "must-never-enter-the-receipt"},
            },
        ],
        "reference": {"actions": [[1, 2, 3]]},
    }


def test_full_receipt_contains_identity_and_booleans_only(workspace):
    settings, config, root = workspace
    package = axis_package(root / "package")
    before = package_digest(package)
    path = write_receipt(settings, config, full_result(before), scope="full")
    data = json.loads(path.read_text())
    assert data["schema_version"] == 1
    assert data["scope"] == "full" and data["accepted"]
    assert data["package_digest"] == before
    assert data["checked_at"] and data["workspace_id"]
    assert data["competition"]["track"] == "task"
    assert data["competition"]["seq"] == 1
    assert data["admission_version"] == "task-source-miner/1"
    assert data["runtime_version"] == "0.3.20"
    assert data["image_digest"] == "sha256:" + "1" * 64
    assert data["checks"] == [
        {"name": "runtime.solvable", "passed": True, "kind": "runtime"}
    ]
    assert "must-never-enter" not in path.read_text()
    assert "reference" not in data
    assert not path.is_relative_to(package)
    assert package_digest(package) == before


def test_static_receipt_has_no_claimed_running_image(workspace):
    settings, config, _root = workspace
    path = write_receipt(
        settings, config, {"ok": True, "package_digest": "a" * 64}, scope="static-only"
    )
    data = json.loads(path.read_text())
    assert data["scope"] == "static-only"
    assert data["image_digest"] is None


def test_definition_only_and_failed_check_cannot_issue_a_receipt(workspace):
    settings, config, _root = workspace
    assert write_receipt(settings, config, {"ok": True}, scope="static-only") is None
    assert (
        write_receipt(
            settings, config, {"ok": False, "package_digest": "a" * 64}, scope="full"
        )
        is None
    )
    assert not receipt_path(settings, config).exists()


def test_bad_full_result_cannot_be_mislabeled_as_physics(workspace):
    settings, config, _root = workspace
    result = full_result("a" * 64)
    result["checks"][0]["passed"] = False
    with pytest.raises(ConfigError, match="successful runtime checks"):
        write_receipt(settings, config, result, scope="full")
    assert not receipt_path(settings, config).exists()


@pytest.mark.parametrize(
    "arguments",
    [
        ["check", "--static-only"],
        ["check", "--example"],
        ["check", "--repo", "author/package"],
    ],
)
def test_every_failed_attempt_removes_the_old_success(workspace, arguments):
    settings, config, root = workspace
    path = write_receipt(settings, config, full_result("a" * 64), scope="full")
    (root / "task.json").write_text('{"schema_version":99}')
    assert main([*arguments, "--config", config]) == 1
    assert not path.exists()


def test_explicit_static_package_success_writes_detached_receipt(workspace):
    settings, config, root = workspace
    package = axis_package(root / "package")
    settings.competition_params["task"].pop("execution_profile")
    import yaml

    data = yaml.safe_load(open(config))
    data["competition"]["params"]["task"].pop("execution_profile")
    open(config, "w").write(yaml.safe_dump(data))
    digest = package_digest(package)
    assert main(["check", str(package), "--static-only", "--config", config]) == 0
    receipt = json.loads(receipt_path(settings, config).read_text())
    assert receipt["scope"] == "static-only" and receipt["package_digest"] == digest
    assert package_digest(package) == digest


def test_cannot_write_receipts_inside_the_package(workspace):
    settings, config, root = workspace
    package = root / "package"
    settings.authoring["evidence"] = str(package / "evidence")
    with pytest.raises(ConfigError, match="outside the task package"):
        invalidate_receipt(settings, config, package)


def test_report_write_failure_cannot_leave_a_success_receipt(workspace, monkeypatch):
    from openroboto.commands import task_check

    settings, config, root = workspace
    path = write_receipt(settings, config, full_result("a" * 64), scope="full")
    monkeypatch.setattr(task_check, "_check", lambda *a: full_result("a" * 64))
    assert (
        main(
            [
                "check",
                "--output",
                str(root / "missing" / "report.json"),
                "--config",
                config,
            ]
        )
        == 1
    )
    assert not path.exists()


@pytest.mark.parametrize(
    "options",
    [
        ["--static-only"],
        ["--repo", "author/task"],
        ["--commit", "a" * 40],
        ["--example"],
        ["--script", "solve.py"],
    ],
)
def test_structural_probe_rejects_recording_and_remote_options(workspace, options):
    settings, config, _root = workspace
    path = write_receipt(settings, config, full_result("a" * 64), scope="full")
    assert main(["check", "--structural-only", *options, "--config", config]) == 1
    assert not path.exists()
