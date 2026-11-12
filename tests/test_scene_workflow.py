"""Scene workspace orchestration and receipts; mocked runtime is not physics."""

import json
import shutil
from contextlib import contextmanager

import pytest
from openroboto_protocol.scene_contract import profile_ref

from openroboto import scene_workflow
from openroboto.check_receipt import verify_receipt
from openroboto.cli import main
from openroboto.commands import doctor
from openroboto.config import Settings
from openroboto.scene_package import build_submission, execution_input, load_submission
from openroboto.task_runtime import TaskRuntime
from tests.task_workspace_fixture import write_task_config


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = write_task_config(tmp_path, profile=profile_ref())
    for name, content in scene_workflow.starter_files().items():
        path = tmp_path / name
        path.parent.mkdir(exist_ok=True)
        path.write_text(content)
    return tmp_path, config


def test_scene_source_copies_only_declared_bytes_and_static_never_starts_docker(
    workspace, monkeypatch, capsys
):
    root, config = workspace
    (root / "solve.py").write_text("private_solver_marker")
    monkeypatch.setattr(TaskRuntime, "prepare", lambda *a, **kw: pytest.fail("Docker"))
    assert main(["check", "--config", str(config), "--static-only", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["ok"] and not result["ready_to_submit"]
    snapshot = next((root / "evidence").glob("scene-check-*/source"))
    _, data = execution_input(snapshot)
    assert set(data) == {"task.json", "scene.xml", "assets/panel.obj"}
    assert "private_solver_marker" not in json.dumps(result)
    (root / "assets/panel.obj").write_text("altered")
    assert main(["check", "--config", str(config), "--json"]) == 1
    result = json.loads(capsys.readouterr().out)
    assert not result["ready_to_submit"] and "dependency" in result["error"]
    assert not (root / "evidence/check-receipt.json").exists()


def fake_runtime(monkeypatch, *, fail_record=False, fail_test=False, input_file=None):
    def prepare(self, **kw):
        self.image = "sha256:" + "1" * 64

    @contextmanager
    def bridge(self, mounts):
        if input_file is not None and any(p.name == "solve.py" for p, _ in mounts):
            assert (input_file, False) in mounts
            assert not any(p == input_file.parent for p, _ in mounts)
        yield "synthetic-runtime-transport"

    def render(args, **kw):
        shutil.copytree(args.execution, args.prepared_execution)
        return {"ok": True}

    calls = []

    def author(args):
        calls.append(args.scene_command)
        if args.scene_command == "record-reference":
            if fail_record:
                return {
                    "ok": False,
                    "issues": [
                        {
                            "code": "scene_recording_failed",
                            "message": "Synthetic recording refusal",
                        }
                    ],
                }
            reference = args.execution.parent.parent / "reference.json"
            reference.write_text(
                '{"schema_version":1,"actions":[],"private_marker":"keep-private"}'
            )
            prepared = args.execution.parent.parent / "bound"
            build_submission(args.execution, prepared, reference=reference)
            shutil.move(prepared / "private", args.private)
            shutil.move(prepared / "submission.json", args.manifest)
            return {"ok": True}
        return {
            "ok": not fail_test,
            "local_physics": "failed" if fail_test else "passed",
            "checks": [{"name": "runtime.solvable", "passed": not fail_test}],
            "issues": [],
            "execution_contract_digest": "c" * 64,
            "runtime_identity_digest": "d" * 64,
        }

    monkeypatch.setattr(TaskRuntime, "prepare", prepare)
    monkeypatch.setattr(TaskRuntime, "python_bridge", bridge)
    monkeypatch.setattr(scene_workflow, "run_instruction", render)
    monkeypatch.setattr(scene_workflow, "run_authoring", author)
    return calls


@pytest.mark.parametrize("failure", [None, "record", "checker", "missing"])
def test_full_scene_receipt_binds_both_roots_and_failed_checks_remove_it(
    workspace, monkeypatch, capsys, failure
):
    root, config = workspace
    if failure != "missing":
        (root / "solve.py").write_text("private_source")
    calls = fake_runtime(
        monkeypatch, fail_record=failure == "record", fail_test=failure == "checker"
    )
    receipt = root / "evidence/check-receipt.json"
    receipt.parent.mkdir()
    receipt.write_text("stale-success")
    assert main(["check", "--config", str(config), "--json"]) == (
        0 if failure is None else 1
    )
    result = json.loads(capsys.readouterr().out)
    assert result["ready_to_submit"] is (failure is None)
    if failure:
        assert not receipt.exists()
        assert "openroboto check" in result["next"]
        return
    original = load_submission(root / "task-package")
    saved = json.loads(receipt.read_text())
    assert saved["submission_digest"] == original.manifest.submission_digest
    assert saved["private_package_digest"] == original.manifest.private_package_digest
    assert (
        "keep-private" not in receipt.read_text()
        and "private_source" not in receipt.read_text()
    )
    assert calls == ["record-reference", "self-test"]
    assert main(["check", "--config", str(config), "--json"]) == 0
    capsys.readouterr()
    assert calls == ["record-reference", "self-test", "self-test"]
    assert load_submission(root / "task-package").manifest == original.manifest
    settings = Settings.load(str(config))
    from types import SimpleNamespace

    live = SimpleNamespace(
        track="task",
        seq=1,
        params={
            "task": {
                "source": "axis",
                "execution_profile": profile_ref(),
                "check_receipt": {"max_age_seconds": 3600},
                "runtime": {"admission_version": "task-source-miner/1"},
            }
        },
    )
    assert (
        verify_receipt(
            settings,
            live,
            original.manifest.execution_package_digest,
            submission_digest=original.manifest.submission_digest,
        )["scope"]
        == "full"
    )
    with pytest.raises(Exception, match="both scene roots"):
        verify_receipt(
            settings,
            live,
            original.manifest.execution_package_digest,
            submission_digest="0" * 64,
        )


def test_create_rebinds_new_source_without_copying_old_proofs(workspace, capsys):
    root, config = workspace
    source = root / "original"
    scene_workflow.source_execution(root / "task.json", source)
    task_path = source / "task.json"
    task = json.loads(task_path.read_text())
    task["environment"]["version"] = "old-runtime"
    task_path.write_text(json.dumps(task))
    before = task_path.read_bytes()
    output = root / "authored/task.json"
    assert (
        main(
            [
                "create",
                str(output),
                "--config",
                str(config),
                "--from-execution",
                str(source),
            ]
        )
        == 0
    )
    capsys.readouterr()
    current = json.loads(output.read_text())
    assert current["environment"] != task["environment"]
    assert current["scene"] == task["scene"] and current["checker"] == task["checker"]
    assert task_path.read_bytes() == before
    assert not (output.parent / "private").exists()
    assert (
        main(
            [
                "create",
                str(output),
                "--config",
                str(config),
                "--from-execution",
                str(source),
            ]
        )
        == 1
    )
    assert task_path.read_bytes() == before
    execution_input(output.parent)


def test_doctor_checks_scene_environment_against_official_bundle(
    workspace, monkeypatch
):
    root, config = workspace
    fake_runtime(monkeypatch)
    settings = Settings.load(str(config))
    assert all(row.ok for row in doctor.check_task_workspace(settings, str(config)))
    value = json.loads((root / "task.json").read_text())
    value["environment"]["version"] = "wrong-runtime"
    (root / "task.json").write_text(json.dumps(value))
    assert not doctor.check_task_workspace(settings, str(config))[0].ok


def test_explicit_solver_data_mounts_only_declared_files_and_static_rejects_p_drift(
    workspace, monkeypatch, capsys
):
    import yaml

    root, config = workspace
    (root / "solve.py").write_text("private source")
    data = root / "public-input.json"
    data.write_text('{"private_auxiliary":"must-not-upload"}')
    value = yaml.safe_load(config.read_text())
    value["authoring"]["solve_inputs"] = [data.name]
    config.write_text(yaml.safe_dump(value))
    fake_runtime(monkeypatch, input_file=data)
    assert main(["check", "--config", str(config), "--json"]) == 0
    capsys.readouterr()
    package = root / "task-package"
    assert not (package / data.name).exists()
    assert "must-not-upload" not in (root / "evidence/check-receipt.json").read_text()
    (package / "private/reference.json").write_text(
        '{"schema_version":1,"actions":[],"altered":true}'
    )
    assert (
        main(
            ["check", str(package), "--config", str(config), "--static-only", "--json"]
        )
        == 1
    )
    assert "private_drift" in capsys.readouterr().out
    assert not (root / "evidence/check-receipt.json").exists()
    assert (
        main(
            [
                "record-reference",
                "--script",
                str(root / "solve.py"),
                "--config",
                str(config),
            ]
        )
        == 0
    )
    load_submission(package)
