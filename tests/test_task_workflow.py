"""Track workspace generation and the complete free check orchestration."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from openroboto_protocol.scene_contract import profile_ref as scene_profile
from openroboto_protocol.tabletop_v2 import profile_ref as v2_profile
from openroboto_protocol.task_contract import execution_profile_ref
from openroboto_protocol.task_profiles import validate_task_document

from openroboto.cli import main
from openroboto.commands import doctor, init, task_check
from openroboto.config import Settings
from openroboto.task_package import package_digest
from openroboto.task_runtime import TaskRuntime, resolve_runtime
from openroboto.task_workspace import catalog_for, definition_for, json_text
from tests.task_workspace_fixture import write_task_config


@pytest.mark.parametrize(
    "profile", [execution_profile_ref(), v2_profile(), scene_profile()]
)
def test_init_generates_the_exact_season_catalog_and_valid_template(
    tmp_path, monkeypatch, profile
):
    from openroboto_protocol.schemas import Competition

    row = Competition.model_validate(
        {
            "id": 274,
            "track": "task",
            "seq": 1,
            "label": "Task",
            "adapter": "task",
            "status": "active",
            "params": {"task": {"source": "axis", "execution_profile": profile}},
        }
    )
    seen = []

    def fetch(url, **kwargs):
        seen.append(kwargs)
        return SimpleNamespace(data=[row])

    monkeypatch.setattr(init, "fetch_competitions", fetch)
    monkeypatch.setattr("openroboto.scene_api.fetch_scene_profiles", lambda *a: [])
    target = tmp_path / "workspace"
    assert (
        main(["init", str(target), "--backend-url", "https://api-dev.openroboto.ai"])
        == 0
    )
    assert seen == [{"include_task": True}]
    expected = {
        "miner.yaml",
        "task.json",
        "catalog.json",
        "README.md",
        ".gitignore",
    }
    if profile == scene_profile():
        from openroboto.scene_workflow import source_execution

        expected.update({"scene.xml", "assets"})
        source_execution(target / "task.json", tmp_path / "execution")
    else:
        assert not validate_task_document(
            json.loads((target / "task.json").read_text()), require_profile=True
        )
    assert {p.name for p in target.iterdir()} == expected
    assert json.loads((target / "catalog.json").read_text()) == catalog_for(profile)
    config = Settings.load(str(target / "miner.yaml"))
    assert (
        config.competition["track"] == "task"
        and config.authoring["package"] == "task-package"
    )
    assert config.authoring["runtime"]["version"] == "0.3.20"
    assert "train_strategy.py" not in (target / "README.md").read_text()


def test_init_eof_writes_nothing(tmp_path, monkeypatch):
    from openroboto_protocol.schemas import Competition

    row = Competition.model_validate(
        {
            "id": 1,
            "track": "task",
            "seq": 1,
            "label": "Task",
            "adapter": "task",
            "status": "active",
            "params": {},
        }
    )
    monkeypatch.setattr(
        init, "fetch_competitions", lambda *a, **kw: SimpleNamespace(data=[row, row])
    )

    def eof(*a):
        raise EOFError

    monkeypatch.setattr("builtins.input", eof)
    target = tmp_path / "unwritten"
    assert main(["init", str(target)]) == 1
    assert not target.exists()


@pytest.mark.parametrize("extended", [False, True])
def test_scene_init_and_import_use_the_published_panel_contract(
    tmp_path, monkeypatch, extended
):
    from openroboto_protocol.schemas import Competition

    from openroboto.scene_workflow import starter_files

    profile = scene_profile()
    contract = json.loads(starter_files()["task.json"])["execution_contract"]
    contract["budget"]["max_api_calls"] = 24
    if extended:
        contract["resource_profile"] = "tabletop-extended-v2"
        contract["budget"] = {
            "max_api_calls": 64,
            "max_steps": 2000,
            "wall_seconds": 900.0,
        }
    row = Competition.model_validate(
        {
            "id": 274,
            "track": "task",
            "seq": 1,
            "label": "Scene",
            "adapter": "task",
            "status": "active",
            "params": {"task": {"source": "axis", "execution_profile": profile}},
        }
    )
    monkeypatch.setattr(
        init, "fetch_competitions", lambda *a, **kw: SimpleNamespace(data=[row])
    )
    monkeypatch.setattr(
        "openroboto.scene_api.fetch_scene_profiles",
        lambda *a: [
            {
                "execution_profile": profile,
                "execution_contract": contract,
            }
        ],
    )
    target = tmp_path / "workspace"
    assert (
        main(["init", str(target), "--backend-url", "https://api-dev.openroboto.ai"])
        == 0
    )
    config = str(target / "miner.yaml")
    settings = Settings.load(config)
    assert settings.authoring["execution_contract"] == contract
    assert (
        json.loads((target / "task.json").read_text())["execution_contract"] == contract
    )
    assert (
        json.loads((target / "catalog.json").read_text())["profiles"][0][
            "execution_contract"
        ]
        == contract
    )
    original = tmp_path / "original"
    from openroboto.scene_workflow import source_execution

    # The imported E keeps its scene/checker but adopts the published NEW contract.
    for name, text in starter_files().items():
        if name == "task.json" and extended:
            source = json.loads(text)
            source["task_definition"]["resource_profile"] = contract["resource_profile"]
            source["checker"]["checker"]["definition"]["resource_profile"] = contract[
                "resource_profile"
            ]
            source["execution_contract"] = contract
            text = json_text(source)
        p = original / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    imported = target / "authored/task.json"
    assert (
        main(
            [
                "create",
                str(imported),
                "--config",
                config,
                "--from-execution",
                str(original),
            ]
        )
        == 0
    )
    assert json.loads(imported.read_text())["execution_contract"] == contract
    source_execution(imported, tmp_path / "bound-execution")

    value = json.loads((target / "task.json").read_text())
    value["execution_contract"]["budget"]["max_api_calls"] = 25
    (target / "task.json").write_text(json.dumps(value))
    assert main(["check", "--config", config, "--static-only"]) == 1


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = write_task_config(tmp_path)
    (tmp_path / "task.json").write_text(
        json_text(definition_for(execution_profile_ref()))
    )
    return tmp_path, config


def test_static_and_missing_reference_never_start_docker(
    workspace, monkeypatch, capsys
):
    root, _config = workspace

    def forbidden(*a, **kw):
        raise AssertionError("Docker must not start")

    monkeypatch.setattr(TaskRuntime, "prepare", forbidden)
    assert main(["check", "--static-only", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["ready_to_submit"] is False
    assert not (root / "evidence").exists()
    assert main(["check", "--json"]) == 1
    result = json.loads(capsys.readouterr().out)
    assert str(root / "solve.py") in result["error"]
    assert "Missing reference" in result["error"]


@pytest.mark.parametrize(
    "failure",
    [None, "solvable", "not_freebie", "deterministic", "reference_covers_instances"],
)
def test_check_preserves_worker_verdict_and_reference_digest(
    workspace, monkeypatch, capsys, failure
):
    root, _config = workspace
    calls = []
    check = {
        "name": failure or "runtime.solvable",
        "passed": not failure,
        "kind": "structural" if failure else "runtime",
        "detail": {"reason": failure or "all instances solved"},
    }

    def prepare(self):
        self.image = "sha256:real-immutable-image"

    def run(self, arguments, mounts, **kwargs):
        calls.append(arguments[0])
        if arguments[0] == "build":
            output = Path(arguments[arguments.index("--output") + 1])
            output.mkdir()
            (output / "task.json").write_text("{}")
            return {"reference_ready": False}
        if arguments[0] == "record-reference":
            (Path(arguments[1]) / "reference.json").write_text(
                '{"instances":{"0":{"actions":[[1]]}}}'
            )
            return {"reference_ready": True}
        if arguments[0] == "admit-static":
            return {"accepted": not failure, "checks": [check] if failure else []}
        return {"accepted": True, "checks": [check]}

    monkeypatch.setattr(TaskRuntime, "prepare", prepare)
    monkeypatch.setattr(TaskRuntime, "run", run)
    assert main(["check", "--example", "--json"]) == (1 if failure else 0)
    result = json.loads(capsys.readouterr().out)
    assert result["checks"] == [check]
    assert result["ready_to_submit"] is (failure is None)
    assert result["image_digest"] == "sha256:real-immutable-image"
    assert result["digest_before_reference"] != result["package_digest"]
    assert package_digest(root / "task-package") == result["package_digest"]
    assert Path(result["report"]).is_file()
    assert not (root / "task-package" / "verdict.json").exists()
    assert calls == ["build", "record-reference", "admit-static"] + (
        [] if failure else ["self-test"]
    )


def test_bad_structure_and_profile_stop_before_docker(workspace, monkeypatch, capsys):
    root, _config = workspace
    (root / "task.json").write_text('{"schema_version":99}')

    def forbidden(*a, **kw):
        raise AssertionError("bad structure invoked Docker")

    monkeypatch.setattr(TaskRuntime, "prepare", forbidden)
    assert main(["check", "--example", "--json"]) == 1
    assert "schema_version" in json.loads(capsys.readouterr().out)["error"]


def test_runtime_mismatch_is_actionable(workspace):
    _root, config = workspace
    settings = Settings.load(str(config))
    settings.competition_params["task"]["runtime"] = {"version": "wrong"}
    with pytest.raises(Exception, match="Install the season's CLI release"):
        resolve_runtime(settings)


def test_doctor_checks_task_package_and_runtime_without_training_gpu(
    workspace, monkeypatch
):
    root, config = workspace

    def prepare(self, **kw):
        self.image = "sha256:verified"

    monkeypatch.setattr(TaskRuntime, "prepare", prepare)
    reports = doctor.check_task_workspace(Settings.load(str(config)), str(config))
    assert all(r.ok for r in reports)
    (root / "task.json").write_text("bad json")
    reports = doctor.check_task_workspace(Settings.load(str(config)), str(config))
    assert not reports[0].ok and "catalog.json" in reports[0].fix


def test_image_metadata_mismatch_is_rejected(workspace, monkeypatch):
    import subprocess

    _root, config = workspace

    def run(argv, **kw):
        return subprocess.CompletedProcess(
            argv, 0, json.dumps([{"Config": {"Labels": {}}}])
        )

    monkeypatch.setattr("openroboto.task_runtime.subprocess.run", run)
    runtime = TaskRuntime(Settings.load(str(config)))
    with pytest.raises(Exception, match="does not match this release"):
        runtime.prepare()


def test_static_package_uses_protocol_and_season_source(tmp_path):
    from tests.test_task_package import axis_package

    package = axis_package(tmp_path / "package")
    settings = Settings(
        competition_params={"task": {"source": "axis", "execution_profile": None}}
    )
    assert task_check.static_package(package, settings)["ok"]
    assert task_check._has_reference(package)
    reference = json.loads((package / "reference.json").read_text())
    reference["instances"]["0"]["actions"] = []
    (package / "reference.json").write_text(json.dumps(reference))
    assert not task_check._has_reference(package)
    settings.competition_params["task"]["source"] = "robodojo"
    report = task_check.static_package(package, settings)
    assert not report["ok"] and report["issues"][-1]["code"] == "source_mismatch"


def test_record_reference_runs_script_in_a_working_copy(workspace, monkeypatch):
    root, _config = workspace
    package = root / "task-package"
    package.mkdir()
    (package / "task.json").write_text("{}")
    (package / "reference.json").write_text("old-reference")
    script = root / "solve.py"
    script.write_text("def solve(runtime, task, instance): pass\n")
    original = (package / "task.json").read_bytes()
    monkeypatch.setattr(TaskRuntime, "prepare", lambda self: None)

    def run(self, command, mounts, **kw):
        work = Path(command[1])
        assert work != package
        assert mounts == [(work, True), (script, False)]
        (work / "reference.json").write_text("new-reference")
        return {"reference_ready": True}

    monkeypatch.setattr(TaskRuntime, "run", run)
    assert main(["record-reference", "--script", str(script)]) == 0
    assert (package / "reference.json").read_text() == "new-reference"
    assert (package / "task.json").read_bytes() == original
    monkeypatch.setattr(
        TaskRuntime,
        "run",
        lambda *a, **kw: {"accepted": False, "error": "checker failed"},
    )
    assert main(["record-reference", "--script", str(script)]) == 1
    assert (package / "reference.json").read_text() == "new-reference"


def test_self_test_delegates_current_verdict_and_report(workspace, monkeypatch, capsys):
    root, _config = workspace
    package = root / "task-package"
    package.mkdir()
    (package / "task.json").write_text("{}")
    monkeypatch.setattr(
        task_check,
        "static_package",
        lambda *a: {"ok": True, "issues": [], "checks": []},
    )
    monkeypatch.setattr(task_check, "_has_reference", lambda *a: True)
    monkeypatch.setattr(
        TaskRuntime, "prepare", lambda self: setattr(self, "image", "sha256:verified")
    )
    check = {
        "name": "runtime.solvable",
        "passed": True,
        "kind": "runtime",
        "detail": {},
    }
    monkeypatch.setattr(
        TaskRuntime, "run", lambda *a, **kw: {"accepted": True, "checks": [check]}
    )
    before = package_digest(package)
    report = root / "report.json"
    assert main(["self-test", "--output", str(report)]) == 0
    assert json.loads(report.read_text())["ready_to_submit"]
    assert package_digest(package) == before
    assert main(["self-test", "--output", str(report)]) == 1
    assert main(["self-test", "--output", str(package / "report.json")]) == 1
    assert not (package / "report.json").exists()


@pytest.mark.parametrize("track", ["task", "sim"])
def test_doctor_routes_package_and_runtime_without_model_gpu_in_task(
    workspace, monkeypatch, track
):
    _root, config = workspace
    settings = Settings.load(str(config))
    settings.competition["track"] = track
    monkeypatch.setattr(doctor.Settings, "load", lambda *a: settings)
    passed = doctor.CheckResult("fixture", True, "ok")
    calls = []
    for name in ("check_python", "check_protocol", "check_docker"):
        monkeypatch.setattr(doctor, name, lambda: passed)
    for name in (
        "check_competition",
        "check_protocol_capabilities",
        "check_hf_token",
        "check_wallet",
    ):
        monkeypatch.setattr(doctor, name, lambda settings: passed)
    monkeypatch.setattr(doctor, "check_settings", lambda settings: [])
    monkeypatch.setattr(doctor, "check_gpu", lambda: calls.append("gpu") or passed)
    monkeypatch.setattr(
        doctor, "check_image", lambda *a: calls.append("training-image") or passed
    )
    monkeypatch.setattr(
        doctor,
        "check_task_workspace",
        lambda *a: calls.append("task-workspace") or [passed],
    )
    assert main(["doctor"]) == 0
    assert calls == (
        ["task-workspace"] if track == "task" else ["gpu", "training-image"]
    )
