"""Pre-payment receipt validation against the exact remote commit digest."""

import copy
import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
import yaml

from openroboto.check_receipt import verify_receipt, write_receipt
from openroboto.commands import submit
from openroboto.config import ConfigError, Settings
from openroboto.task_package import package_digest
from tests.task_workspace_fixture import write_task_config
from tests.test_check_receipt import full_result
from tests.test_task_package import axis_package


@pytest.fixture
def checked(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = write_task_config(tmp_path)
    raw = yaml.safe_load(config.read_text())
    raw["competition"]["params"]["task"]["execution_profile"] = None
    config.write_text(yaml.safe_dump(raw))
    settings = Settings.load(str(config))
    package = axis_package(tmp_path / "remote")
    digest = package_digest(package)
    path = write_receipt(settings, str(config), full_result(digest), scope="full")
    receipt = json.loads(path.read_text())
    now = datetime.fromisoformat(receipt["checked_at"]) + timedelta(seconds=1)
    params = copy.deepcopy(settings.competition_params)
    params["task"]["check_receipt"] = {"max_age_seconds": 60}
    params["task"]["runtime"] = {"admission_version": "task-source-miner/1"}
    live = SimpleNamespace(track="task", seq=1, label="Task", params=params)
    return settings, live, package, digest, path, now


def test_valid_receipt_never_executes_a_runtime(checked, monkeypatch):
    settings, live, _package, digest, _path, now = checked

    def forbidden(*a, **kw):
        raise AssertionError("submit must not rerun physics")

    monkeypatch.setattr("openroboto.task_runtime.TaskRuntime.prepare", forbidden)
    assert verify_receipt(settings, live, digest, now=now)["scope"] == "full"


@pytest.mark.parametrize(
    "change,reason",
    [
        ("missing", "No successful check receipt"),
        ("digest", "digest differs"),
        ("static-only", "static-only"),
        ("admission", "admission_version"),
        ("expired", "expired"),
        ("workspace", "another workspace"),
        ("competition", "different season"),
        ("future", "future"),
        ("invalid-json", "Cannot read"),
        ("bad-time", "checked_at"),
        ("naive-time", "checked_at"),
        ("failed-check", "failed or invalid"),
        ("image", "immutable image digest"),
        ("runtime", "runtime_version"),
        ("scope", "valid scope"),
    ],
)
def test_invalid_receipts_refuse_with_actionable_reason(checked, change, reason):
    settings, live, _package, digest, path, now = checked
    data = json.loads(path.read_text())
    if change == "missing":
        path.unlink()
    elif change == "digest":
        data["package_digest"] = "b" * 64
    elif change == "static-only":
        data["scope"] = "static-only"
    elif change == "admission":
        data["admission_version"] = "other"
    elif change == "expired":
        data["checked_at"] = (now - timedelta(seconds=60)).isoformat()
    elif change == "workspace":
        data["workspace_id"] = "other"
    elif change == "competition":
        data["competition"]["seq"] = 2
    elif change == "future":
        data["checked_at"] = (now + timedelta(seconds=1)).isoformat()
    elif change == "bad-time":
        data["checked_at"] = None
    elif change == "naive-time":
        data["checked_at"] = "2026-11-12T00:00:00"
    elif change == "failed-check":
        data["checks"][0]["passed"] = False
    elif change == "image":
        data["image_digest"] = None
    elif change == "runtime":
        data["runtime_version"] = "other"
    elif change == "scope":
        data["scope"] = []
    if change == "invalid-json":
        path.write_text("bad json")
    elif change != "missing":
        path.write_text(json.dumps(data))
    with pytest.raises(ConfigError, match=reason):
        verify_receipt(settings, live, digest, now=now)


def test_only_live_policy_can_allow_static_receipts(checked, capsys):
    settings, live, _package, digest, path, now = checked
    data = json.loads(path.read_text())
    data["scope"] = "static-only"
    data["image_digest"] = None
    path.write_text(json.dumps(data))
    settings.competition_params["task"]["check_receipt"] = {"allow_static_only": True}
    with pytest.raises(ConfigError, match="static-only"):
        verify_receipt(settings, live, digest, now=now)
    live.params["task"]["check_receipt"]["allow_static_only"] = True
    assert verify_receipt(settings, live, digest, now=now)["scope"] == "static-only"
    assert submit.check_receipt_is_payable(settings, {}, live, digest)
    assert "explicitly allows" in capsys.readouterr().out


@pytest.mark.parametrize("ttl", [None, 0, -1, True, "60"])
def test_operator_must_declare_a_valid_lifetime(checked, ttl):
    settings, live, _package, digest, _path, now = checked
    live.params["task"]["check_receipt"]["max_age_seconds"] = ttl
    with pytest.raises(ConfigError, match="operator"):
        verify_receipt(settings, live, digest, now=now)


def test_operator_must_publish_the_current_worker_admission_version(checked):
    settings, live, _package, digest, _path, now = checked
    live.params["task"].pop("runtime")
    with pytest.raises(ConfigError, match="publish the active worker"):
        verify_receipt(settings, live, digest, now=now)


def test_current_live_runtime_and_image_binding(checked):
    settings, live, _package, digest, _path, now = checked
    live.params["task"]["runtime"] = {"admission_version": "future"}
    with pytest.raises(ConfigError, match="Install the season"):
        verify_receipt(settings, live, digest, now=now)
    live.params["task"]["runtime"] = {
        "admission_version": "task-source-miner/1",
        "image_digest": "sha256:" + "2" * 64,
    }
    with pytest.raises(ConfigError, match="image digest differs"):
        verify_receipt(settings, live, digest, now=now)


def test_receipt_does_not_replace_remote_structure_validation(
    checked, monkeypatch, capsys
):
    settings, live, package, _digest, path, _now = checked
    state = {"hf_repo_id": "author/task", "hf_commit": "a" * 40}
    monkeypatch.setattr(submit, "fetch_package", lambda *a, **kw: package)
    assert submit.task_package_is_payable(settings, state, live)
    (package / "benchmark.json").write_text("{}")
    # Even a digest-matched receipt must not waive structure checks.
    data = json.loads(path.read_text())
    data["package_digest"] = package_digest(package)
    path.write_text(json.dumps(data))
    assert not submit.task_package_is_payable(settings, state, live)
    error = capsys.readouterr().err
    assert "not well formed" in error
    assert f"openroboto check --repo author/task --commit {'a' * 40}" in error


def test_same_local_receipt_must_match_the_uploaded_commit(checked, monkeypatch):
    settings, live, package, _digest, _path, _now = checked
    monkeypatch.setattr(submit, "fetch_package", lambda *a, **kw: package)
    (package / "extra.txt").write_text("uploaded contents differ")
    state = {"hf_repo_id": "author/task", "hf_commit": "a" * 40}
    assert not submit.task_package_is_payable(settings, state, live)


def test_scene_receipt_must_bind_the_joint_identity(checked):
    settings, live, _package, digest, path, now = checked
    with pytest.raises(ConfigError, match="both scene roots"):
        verify_receipt(settings, live, digest, submission_digest="c" * 64, now=now)
    data = json.loads(path.read_text())
    data["submission_digest"] = "c" * 64
    path.write_text(json.dumps(data))
    assert verify_receipt(settings, live, digest, submission_digest="c" * 64, now=now)


@pytest.mark.parametrize("fee_kind", ["burn", "gamma"])
@pytest.mark.parametrize(
    "failure", [None, "missing", "digest", "static-only", "version", "expired"]
)
def test_submit_gates_payment_with_the_actual_receipt(
    checked, monkeypatch, failure, fee_kind
):
    from openroboto.cli import main

    settings, live, package, _digest, path, now = checked
    settings.authoring["package"] = str(package)
    if fee_kind == "gamma":
        settings.competition["params"]["fee"] = {"kind": "gamma", "amount_gamma": "25"}
        live.params["fee"] = {"kind": "gamma", "amount_gamma": "25"}
    if failure == "missing":
        path.unlink()
    elif failure:
        data = json.loads(path.read_text())
        if failure == "digest":
            data["package_digest"] = "b" * 64
        if failure == "static-only":
            data["scope"] = "static-only"
        if failure == "version":
            data["admission_version"] = "other"
        if failure == "expired":
            data["checked_at"] = (now - timedelta(seconds=61)).isoformat()
        path.write_text(json.dumps(data))
    state = {}
    events = []
    verdict = SimpleNamespace(live=live, cid=274, kind=fee_kind)
    monkeypatch.setattr(submit.Settings, "load", lambda *a: settings)
    monkeypatch.setattr(submit, "load_state", lambda *a: state)
    monkeypatch.setattr(submit, "save_state", lambda *a: None)
    monkeypatch.setattr(submit, "resolve_competition", lambda *a: verdict)
    monkeypatch.setattr(submit, "fetch_package", lambda *a, **kw: package)

    def upload(settings, cid, directory, state, **kw):
        assert directory == str(package)
        events.append("upload")
        state.update(hf_repo_id="author/task", hf_commit="a" * 40)

    monkeypatch.setattr(submit, "perform_upload", upload)
    for name in ("contributor_is_bound", "slot_is_free", "evaluator_can_read"):
        monkeypatch.setattr(submit, name, lambda *a: True)
    monkeypatch.setattr(submit, "confirm_payment", lambda *a: events.append("confirm"))

    def pay(*a, **kw):
        events.append("pay")
        state["burn_tx_hash"] = "fixture-payment"
        return True

    monkeypatch.setattr(submit, "perform_burn", pay)
    monkeypatch.setattr(submit, "perform_gamma", pay)
    monkeypatch.setattr(
        submit, "perform_announce", lambda *a: events.append("announce") or True
    )

    def forbidden(*a, **kw):
        raise AssertionError("submit must not rerun check")

    monkeypatch.setattr("openroboto.task_runtime.TaskRuntime.prepare", forbidden)
    assert main(
        ["--log-dir", "test-logs", "submit", "--config", settings.config_path]
    ) == (1 if failure else 0)
    assert events == (
        ["upload"] if failure else ["upload", "confirm", "pay", "announce"]
    )
