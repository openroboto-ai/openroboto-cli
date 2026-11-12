"""Container transport boundaries; these mocks are not physics evidence."""

from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from openroboto.config import ConfigError, Settings
from openroboto.task_runtime import TaskRuntime
from tests.task_workspace_fixture import write_task_config


@pytest.mark.parametrize("failure", [None, "start", "authoring", "cleanup"])
def test_owned_container_is_removed_on_success_start_failure_and_authoring_error(
    tmp_path, monkeypatch, failure
):
    execution = tmp_path / "execution"
    output = tmp_path / "evidence"
    execution.mkdir()
    output.mkdir()
    config = write_task_config(tmp_path)
    runtime = TaskRuntime(Settings.load(str(config)))
    runtime.image = "sha256:synthetic-transport-test"
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(
            returncode=int(
                (failure == "start" and command[1] == "run")
                or (failure == "cleanup" and command[1] == "rm")
            ),
            stderr="synthetic start failure",
        )

    monkeypatch.setattr("openroboto.task_runtime.subprocess.run", run)
    wrapper = None
    with pytest.raises(ConfigError) if failure else nullcontext():
        with runtime.python_bridge([(execution, False), (output, True)]) as python:
            wrapper = Path(python)
            assert wrapper.is_file() and wrapper.stat().st_mode & 0o777 == 0o700
            assert not wrapper.is_relative_to(execution)
            assert not wrapper.is_relative_to(output)
            compile(wrapper.read_text(), str(wrapper), "exec")
            assert "docker" in wrapper.read_text() and "exec" in wrapper.read_text()
            if failure == "authoring":
                raise ConfigError("synthetic authoring failure")
    start, stop = calls
    name = start[start.index("--name") + 1]
    assert stop == ["docker", "rm", "-f", name]
    assert "--network=none" in start and "--read-only" in start
    assert "--cap-drop=ALL" in start and "--detach" in start
    assert f"type=bind,src={execution},dst={execution},readonly" in start
    assert f"type=bind,src={output},dst={output}" in start
    assert wrapper is None or not wrapper.exists()
