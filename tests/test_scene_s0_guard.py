"""Pure tests for S0 command isolation; never invoke a worker/native command."""

import hashlib

import pytest

from tests import scene_s0_guard as guard

METADATA = 'print("pure installed registry identity")'
METADATA_SHA = hashlib.sha256(METADATA.encode()).hexdigest()


@pytest.mark.parametrize(
    "argv,kind",
    [
        (["-I", "-c", METADATA], "registry"),
        (
            ["-I", "-m", "task_source.authoring", "describe", "--include-development"],
            "describe",
        ),
        (
            [
                "-I",
                "-m",
                "task_source.authoring",
                "scene-render-instruction",
                "--execution",
                "E",
                "--output",
                "PUBLIC_JSON",
            ],
            "pure_render",
        ),
    ],
)
def test_s0_guard_allows_only_pure_registered_invocations(argv, kind):
    assert guard.classify_invocation(argv, METADATA_SHA) == kind


@pytest.mark.parametrize(
    "argv",
    [
        ["-I", "-m", "task_source.authoring", "scene-record-reference"],
        ["-I", "-m", "task_source.authoring", "scene-self-test"],
        ["-I", "-m", "task_source.authoring", "record-reference"],
        ["-I", "-m", "task_source.authoring", "self-test"],
        ["-I", "-m", "task_source.scene_runtime", "run_scene_process"],
        ["-I", "-c", "import mujoco"],
        ["-S", "-m", "task_source.authoring", "describe"],
        [
            "-I",
            "-m",
            "task_source.authoring",
            "scene-render-instruction",
            "--execution",
            "E",
            "--script",
            "S",
        ],
        [
            "-I",
            "-m",
            "task_source.authoring",
            "scene-render-instruction",
            "--execution",
            "E",
            "--execution",
            "OTHER",
        ],
    ],
)
def test_s0_guard_rejects_record_self_test_and_unknown_dispatch_without_running_it(
    argv,
):
    with pytest.raises(guard.S0Violation):
        guard.classify_invocation(argv, METADATA_SHA)


@pytest.mark.parametrize(
    "name", ["mujoco", "mujoco._simulate", "glfw", "torch", "transformers"]
)
def test_s0_guard_classifies_native_and_model_imports_without_importing_them(name):
    assert guard.blocked_import(name)


@pytest.mark.parametrize(
    "event,args",
    [
        ("subprocess.Popen", ("python",)),
        ("os.fork", ()),
        ("os.posix_spawn", ("python",)),
        ("socket.connect", ("host",)),
        ("socket.getaddrinfo", ("host",)),
        ("ctypes.dlopen", ("/tmp/libmujoco.dylib",)),
    ],
)
def test_s0_guard_classifies_forbidden_effects_without_performing_them(event, args):
    assert guard.blocked_audit_event(event, args)


def test_s0_guard_leaves_regular_pure_python_operations_available():
    assert not guard.blocked_import("json")
    assert not guard.blocked_audit_event("open", ("public-task.json", "r"))


@pytest.mark.parametrize("command", ["record-reference", "self-test"])
def test_s0_guard_rejects_even_parent_cli_authoring_before_launch(command):
    with pytest.raises(guard.S0Violation):
        guard.classify_cli_invocation(["scene", command])


def test_s0_guard_allows_parent_cli_instruction_and_new_E():
    assert (
        guard.classify_cli_invocation(
            [
                "scene",
                "instruction",
                "--execution",
                "E",
                "--output",
                "PUBLIC",
                "--prepared-execution",
                "NEW_E",
                "--runtime-python",
                "PYTHON",
            ]
        )
        == "instruction_and_new_E"
    )


def test_s0_guard_startup_failure_terminates_instead_of_site_continuing(
    monkeypatch, tmp_path
):
    import json
    import sys

    module = tmp_path / "guard.py"
    module.with_suffix(".json").write_text(
        json.dumps({"metadata_sha256": METADATA_SHA})
    )
    monkeypatch.setattr(guard, "__file__", str(module))
    monkeypatch.setattr(
        sys,
        "orig_argv",
        ["python", "-I", "-m", "task_source.authoring", "scene-record-reference"],
    )
    events = []
    monkeypatch.setattr(
        guard, "_event", lambda config, event, **fields: events.append(event)
    )

    def exit_process(code):
        raise SystemExit(code)

    monkeypatch.setattr(guard.os, "_exit", exit_process)
    original_finders = list(sys.meta_path)
    with pytest.raises(SystemExit) as error:
        guard.install()
    assert error.value.code == 97
    assert events == ["startup_rejected"]
    assert sys.meta_path == original_finders


def test_s0_guard_is_idempotent_when_site_revisits_the_same_pth(monkeypatch):
    monkeypatch.setattr(guard, "_INSTALLED", True)

    def unexpected_read(path):
        raise AssertionError("installed guard must not initialize twice")

    monkeypatch.setattr(guard.pathlib.Path, "read_bytes", unexpected_read)
    guard.install()
