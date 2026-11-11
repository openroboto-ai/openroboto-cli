"""The task command surface routes by workspace track and runs Docker."""

import json
from pathlib import Path

import pytest
from openroboto_protocol.task_contract import execution_profile_ref

from openroboto.cli import build_parser, main
from openroboto.task_runtime import TaskRuntime, image_command
from openroboto.task_workspace import catalog_for
from tests.task_workspace_fixture import write_task_config


@pytest.fixture(autouse=True)
def workspace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return write_task_config(tmp_path)


def test_catalog_is_complete_and_filtered_to_workspace(capsys):
    assert main(["catalog", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == catalog_for(execution_profile_ref())
    assert main(["catalog"]) == 0
    assert "franka-panda" in capsys.readouterr().out


def test_create_is_exclusive_and_has_no_public_summary(tmp_path):
    target = tmp_path / "task.json"
    assert main(["create"]) == 0
    doc = json.loads(target.read_text())
    assert doc["execution_profile"] == execution_profile_ref()
    assert "public_summary" not in doc
    before = target.read_bytes()
    assert main(["create"]) == 1
    assert target.read_bytes() == before


def test_create_explicit_public_summary(tmp_path):
    public = tmp_path / "public.json"
    summary = {
        "title": "Title",
        "instruction": "Synopsis",
        "robot": "franka-panda",
        "origin": "original",
        "execution_profile": execution_profile_ref(),
    }
    public.write_text(json.dumps(summary))
    assert main(["create", "--public-summary", str(public)]) == 0
    assert json.loads((tmp_path / "task.json").read_text())["public_summary"] == summary
    public.write_text('{"title":"incomplete"}')
    assert main(["create", "bad.json", "--public-summary", str(public)]) == 1
    assert not (tmp_path / "bad.json").exists()


@pytest.mark.parametrize(
    "change", ["bad-json", "bad-schema", "unknown-field", "bad-profile"]
)
def test_bad_definitions_stop_before_docker(tmp_path, monkeypatch, change):
    assert main(["create"]) == 0
    target = tmp_path / "task.json"
    document = json.loads(target.read_text())
    if change == "bad-json":
        target.write_text("not json")
    else:
        if change == "bad-schema":
            document["schema_version"] = True
        if change == "unknown-field":
            document["gravity"] = [0, 0, 0]
        if change == "bad-profile":
            document["execution_profile"]["digest"] = "0" * 64
        target.write_text(json.dumps(document))

    def forbidden(*a, **kw):
        raise AssertionError("invalid definition invoked Docker")

    monkeypatch.setattr(TaskRuntime, "prepare", forbidden)
    assert main(["build"]) == 1
    assert main(["check", "--static-only"]) == 1


def test_build_uses_official_container_and_keeps_arguments(tmp_path, monkeypatch):
    assert main(["create"]) == 0
    calls = []
    monkeypatch.setattr(TaskRuntime, "prepare", lambda self: None)

    def run(self, command, mounts, **kw):
        calls.append((command, mounts))
        Path(command[command.index("--output") + 1]).mkdir()
        return {"package_digest": "a" * 64}

    monkeypatch.setattr(TaskRuntime, "run", run)
    assert main(["build", "--title", "A title with spaces", "--task-id", "mine"]) == 0
    assert calls[0][0][-4:] == ["--task-id", "mine", "--title", "A title with spaces"]
    assert calls[0][1][0] == (tmp_path / "task.json", False)
    assert (tmp_path / "task-package").is_dir()
    assert main(["build"]) == 1


@pytest.mark.parametrize(
    "command",
    [["catalog"], ["create"], ["record-reference", "--example"], ["self-test"]],
)
def test_task_only_verbs_reject_model_workspace(tmp_path, command):
    (tmp_path / "miner.yaml").write_text("competition: {track: sim}\n")
    assert main(command) == 1


def test_old_group_and_runtime_python_are_removed():
    for argv in (
        ["task", "check"],
        ["init", "--include-task"],
        ["record-reference", "--example", "--runtime-python", "/python"],
    ):
        with pytest.raises(SystemExit) as exc:
            build_parser().parse_args(argv)
        assert exc.value.code == 2


def test_record_and_reports_cannot_be_inside_package(tmp_path, monkeypatch):
    package = tmp_path / "task-package"
    package.mkdir()

    def forbidden(*a, **kw):
        raise AssertionError("invalid path invoked Docker")

    monkeypatch.setattr(TaskRuntime, "prepare", forbidden)
    assert main(["record-reference", "--script", str(package / "solve.py")]) == 1
    assert main(["self-test", "--output", str(package / "report.json")]) == 1


def test_container_mounts_only_explicit_files_and_limits_resources(tmp_path):
    package = tmp_path / "package"
    solve = tmp_path / "solve.py"
    command = image_command(
        "sha256:fixed",
        ["self-test", str(package)],
        [(package, False), (solve, False)],
        "test",
    )
    assert "--network=none" in command and "--read-only" in command
    assert "--cpus=2" in command and "--memory=4g" in command
    assert f"type=bind,src={package},dst={package},readonly" in command
    assert f"type=bind,src={solve},dst={solve},readonly" in command
    assert "miner.yaml" not in " ".join(command)
