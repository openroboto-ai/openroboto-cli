"""Official image identity, failure handling and isolated Docker arguments."""

import json
import subprocess

import pytest

from openroboto.config import ConfigError, Settings
from openroboto.task_runtime import TaskRuntime, resolve_runtime, runtime_release
from tests.task_workspace_fixture import write_task_config


@pytest.fixture
def runtime(tmp_path):
    return TaskRuntime(Settings.load(str(write_task_config(tmp_path))))


def docker_results(runtime, monkeypatch, *, missing=False, build_code=0, mismatch=None):
    calls = []
    inspected = False
    content_label = {
        "org.openroboto.runtime.content-sha256": runtime.release["content_sha256"]
    }

    def run(argv, **kw):
        nonlocal inspected
        calls.append(argv)
        if argv[1:3] == ["image", "inspect"]:
            if missing and not inspected:
                inspected = True
                return subprocess.CompletedProcess(argv, 1, "", "missing image")
            return subprocess.CompletedProcess(
                argv,
                0,
                json.dumps(
                    [
                        {
                            "Id": "sha256:immutable",
                            "Config": {"Labels": content_label},
                        }
                    ]
                ),
                "",
            )
        if argv[1] == "build":
            return subprocess.CompletedProcess(
                argv, build_code, "", "build failed" if build_code else ""
            )
        description = {**runtime.release, "runtime_version": runtime.release["version"]}
        if mismatch:
            description[mismatch] = "wrong"
        return subprocess.CompletedProcess(argv, 0, json.dumps(description), "")

    monkeypatch.setattr("openroboto.task_runtime.subprocess.run", run)
    return calls


def test_prepare_checks_official_metadata_and_runs_immutable_image(
    runtime, monkeypatch
):
    calls = docker_results(runtime, monkeypatch)
    runtime.prepare()
    assert runtime.image == "sha256:immutable"
    assert not any(c[1] == "build" for c in calls)
    assert calls[-1][-2:] == ["sha256:immutable", "describe"]
    assert "--network=none" in calls[-1]


def test_missing_image_is_built_from_the_bundled_context(runtime, monkeypatch):
    calls = docker_results(runtime, monkeypatch, missing=True)
    runtime.prepare()
    build = next(c for c in calls if c[1] == "build")
    assert str(runtime.context) == build[-1]
    assert "RUNTIME_CONTENT_SHA256=" + runtime.release["content_sha256"] in build


def test_doctor_does_not_build_missing_image(runtime, monkeypatch):
    calls = docker_results(runtime, monkeypatch, missing=True)
    with pytest.raises(ConfigError, match="image is missing"):
        runtime.prepare(build=False)
    assert len(calls) == 1


def test_failed_build_is_not_ready(runtime, monkeypatch):
    docker_results(runtime, monkeypatch, missing=True, build_code=1)
    with pytest.raises(ConfigError, match="Task image build failed"):
        runtime.prepare()
    assert runtime.image == ""


@pytest.mark.parametrize(
    "field",
    [
        "runtime_version",
        "protocol_version",
        "execution_profiles",
        "admission_version",
        "admission_policy_sha256",
    ],
)
def test_wrong_runtime_metadata_is_refused(runtime, monkeypatch, field):
    docker_results(runtime, monkeypatch, mismatch=field)
    with pytest.raises(ConfigError, match="mismatch"):
        runtime.prepare()


def test_changed_bundled_wheel_cannot_be_built(runtime, monkeypatch):
    runtime.release["files"] = {"Dockerfile": "0" * 64}
    calls = docker_results(runtime, monkeypatch)
    with pytest.raises(ConfigError, match="artifact changed"):
        runtime.prepare()
    assert not calls


def test_changed_content_manifest_cannot_be_built(runtime, monkeypatch):
    runtime.release["content_sha256"] = "0" * 64
    calls = docker_results(runtime, monkeypatch)
    with pytest.raises(ConfigError, match="content digest"):
        runtime.prepare()
    assert not calls


@pytest.mark.parametrize(
    "payload,code,expect",
    [
        ("not json", 2, "runtime failed"),
        ('{"accepted":false,"error":"reference failed"}', 1, None),
        ('{"anything":true}', 2, "runtime exited"),
    ],
)
def test_runtime_failure_is_not_success(runtime, monkeypatch, payload, code, expect):
    def run(argv, **kw):
        return subprocess.CompletedProcess(argv, code, payload, "simulator unavailable")

    monkeypatch.setattr("openroboto.task_runtime.subprocess.run", run)
    if expect:
        with pytest.raises(ConfigError, match=expect):
            runtime.run(["self-test"])
    else:
        assert runtime.run(["self-test"])["accepted"] is False


def test_timeout_kills_only_its_named_container(runtime, monkeypatch):
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        if argv[1] == "run":
            raise subprocess.TimeoutExpired(argv, kw["timeout"])
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr("openroboto.task_runtime.subprocess.run", run)
    with pytest.raises(ConfigError, match="exceeded 180s"):
        runtime.run(["self-test"])
    name = calls[0][calls[0].index("--name") + 1]
    assert calls[1] == ["docker", "rm", "-f", name]


def test_authoring_and_season_runtime_bindings_are_checked(tmp_path):
    settings = Settings.load(str(write_task_config(tmp_path)))
    release = runtime_release()
    settings.competition_params["task"]["runtime"] = {
        k: release[k] for k in ("version", "content_sha256", "admission_version")
    }
    assert resolve_runtime(settings) == release
    settings.authoring["runtime"] = {"content_sha256": "wrong"}
    with pytest.raises(ConfigError, match=r"authoring\.runtime"):
        resolve_runtime(settings)
