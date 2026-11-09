"""Synthetic runtime responses and child processes, never physics evidence."""

from __future__ import annotations

import copy
import importlib
import json
import shutil
import sys
import time
from pathlib import Path

import pytest
from openroboto_protocol.scene_contract import profile_catalog, profile_ref
from openroboto_protocol.tabletop_assets import canonical_digest

from openroboto.cli import main
from openroboto.scene_package import (
    ScenePackageError,
    build_submission,
    contents_digest,
    execution_input,
    load_submission,
)

FIXTURE = Path(__file__).parent / "fixtures/open_scene"


@pytest.fixture
def runtime_case(tmp_path, monkeypatch):
    bridge = importlib.import_module("openroboto.scene_authoring")
    execution = tmp_path / "source-execution"
    shutil.copytree(FIXTURE / "execution", execution)
    task = json.loads((execution / "task.json").read_text())
    second = copy.deepcopy(task["scene"]["initial_states"][0])
    second["id"] = "second_synthetic_state"
    task["scene"]["initial_states"].append(second)
    registry = {
        "id": "panda-mujoco-scene",
        "version": "synthetic-runtime-identity",
        "runtime": {
            "version": "0.3.0",
            "protocol_version": "0.17.1",
            "source_digest": "a" * 64,
            "execution_profile": profile_ref(),
            "agent": task["execution_contract"]["agent"],
        },
    }
    environment = {
        "id": registry["id"],
        "version": registry["version"],
        "digest": canonical_digest(registry),
    }
    task["environment"] = environment
    (execution / "task.json").write_text(json.dumps(task))
    built = build_submission(
        execution, tmp_path / "submission", private=FIXTURE / "private"
    )
    metadata = {
        "protocol_version": "0.17.1",
        "runtime_version": "0.3.0",
        "profile": profile_catalog(),
        "refs": {
            "environment": environment,
            "artifact": task["robot"]["artifact"],
            "control_mapping": task["robot"]["control_mapping"],
        },
    }
    description = {
        "scene_authoring": {
            "api_version": 1,
            "runtime_version": "0.3.0",
            "execution_profile": profile_ref(),
            "environment": environment,
            "registry": registry,
            "commands": ["scene-record-reference", "scene-self-test"],
            "status": "development",
        }
    }
    case = {
        "bridge": bridge,
        "root": built.root,
        "execution": built.root / "execution",
        "private": tmp_path / "new-private",
        "manifest": tmp_path / "new-manifest.json",
        "output": tmp_path / "public.json",
        "private_report": tmp_path / "private-diagnostics.json",
        "script": tmp_path / "local-solver.py",
        "task": task,
        "metadata": metadata,
        "description": description,
        "calls": [],
        "accepted": True,
        "mutate": None,
        "real_invoke": bridge._invoke,
    }
    case["script"].write_text("raise AssertionError('No solver in synthetic tests')")

    def invoke(python, argv, *, timeout):
        case["calls"].append((python, list(argv), timeout))
        if argv[0] == "-c":
            return 0, copy.deepcopy(case["metadata"])
        if "describe" in argv:
            return 0, copy.deepcopy(case["description"])
        command = argv[2]
        report_path = Path(argv[argv.index("--output") + 1])
        if command == "scene-record-reference":
            private = Path(argv[argv.index("--private") + 1])
            manifest = Path(argv[argv.index("--manifest") + 1])
            # Synthetic binding only: no reference actions or physical runtime.
            temp = build_submission(
                case["execution"],
                tmp_path / "synthetic-result",
                private=FIXTURE / "private",
            )
            shutil.move(str(temp.root / "private"), private)
            shutil.move(str(temp.root / "submission.json"), manifest)
            body = {
                "reference_ready": True,
                "instances": len(task["scene"]["initial_states"]),
                "steps": {
                    str(i): 1 for i in range(len(task["scene"]["initial_states"]))
                },
                "submission": json.loads(manifest.read_text()),
            }
        else:
            total = len(task["scene"]["initial_states"])
            body = {
                "accepted": case["accepted"],
                "physical_checks_complete": True,
                "tool_reference_verified": True,
                "checks": [
                    {"name": name, "passed": case["accepted"], "detail": detail}
                    for name, detail in (
                        ("runtime.solvable", {"total": total}),
                        ("runtime.deterministic", {}),
                        ("runtime.noise", {"total": total, "trials": 1}),
                        ("runtime.nop", {"total": total}),
                        ("runtime.visibility", {}),
                    )
                ],
            }
        body.update(
            ok=True,
            capability="local_physics",
            runtime_version=case["metadata"]["runtime_version"],
            execution_profile=profile_ref(),
            environment=environment,
            registry=registry,
            execution_contract=task["execution_contract"],
            execution_package_digest=contents_digest(
                execution_input(case["execution"])[1]
            ),
            business_model_calls=0,
            source="native_mujoco_local_cpu",
            private_marker="never-publish-this",
        )
        if case["mutate"]:
            case["mutate"](body)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(body))
        return (0 if case["accepted"] else 1), {"private_marker": "never-publish-this"}

    monkeypatch.setattr(bridge, "_invoke", invoke)
    return case


def command(case, name="record-reference"):
    args = [
        "task",
        "scene",
        name,
        "--execution",
        str(case["execution"]),
        "--private",
        str(case["private"]),
        "--manifest",
        str(case["manifest"]),
        "--output",
        str(case["output"]),
        "--private-report",
        str(case["private_report"]),
        "--runtime-python",
        sys.executable,
        "--timeout-seconds",
        "30",
    ]
    if name == "record-reference":
        args += ["--script", str(case["script"])]
    return args


def test_record_uses_execution_new_private_and_detached_manifest(runtime_case, capsys):
    case = runtime_case
    before = {
        p.relative_to(case["execution"]): p.read_bytes()
        for p in case["execution"].rglob("*")
        if p.is_file()
    }
    assert main(command(case)) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["reference_ready"] is True
    assert result["local_physics"] == "reference_recorded"
    assert result["server_admission"] == "not_run"
    assert "never-publish-this" not in json.dumps(result) + case["output"].read_text()
    assert "never-publish-this" in case["private_report"].read_text()
    assert before == {
        p.relative_to(case["execution"]): p.read_bytes()
        for p in case["execution"].rglob("*")
        if p.is_file()
    }
    run = next(argv for _, argv, _ in case["calls"] if "scene-record-reference" in argv)
    assert run[2] == "scene-record-reference"
    assert str(case["private"]) in run and str(case["manifest"]) in run
    assert str(case["output"]) not in run
    assert all(python == sys.executable for python, _, _ in case["calls"])


@pytest.mark.parametrize("accepted", [True, False])
def test_self_test_binds_all_inputs_and_preserves_rejection(
    runtime_case, capsys, accepted
):
    case = runtime_case
    case["private"] = case["root"] / "private"
    case["manifest"] = case["root"] / "submission.json"
    case["accepted"] = accepted
    assert main(command(case, "self-test")) == (0 if accepted else 1)
    result = json.loads(capsys.readouterr().out)
    assert result["accepted"] is accepted
    assert result["physical_checks_complete"] is True
    assert result["local_physics"] == ("passed" if accepted else "failed")
    argv = next(argv for _, argv, _ in case["calls"] if "scene-self-test" in argv)
    manifest = load_submission(case["root"]).manifest
    assert (
        argv[argv.index("--expect-submission-digest") + 1] == manifest.submission_digest
    )
    assert "--max-reference-steps" not in argv
    assert "never-publish-this" not in result.__str__() + case["output"].read_text()


@pytest.mark.parametrize(
    "field,value",
    [
        (("metadata", "protocol_version"), "0.17.0"),
        (("metadata", "runtime_version"), "0.2.0"),
        (("metadata", "profile", "task_schema_version"), 2),
        (("metadata", "profile", "execution_contract_version"), 2),
        (("metadata", "refs", "artifact", "digest"), "0" * 64),
        (("metadata", "refs", "control_mapping", "version"), "unknown"),
        (("description", "scene_authoring", "api_version"), True),
        (("description", "scene_authoring", "commands"), ["record-reference"]),
        (("description", "scene_authoring", "status"), "unsupported"),
        (("description", "scene_authoring", "environment", "digest"), "0" * 64),
        (("description", "scene_authoring", "execution_profile", "version"), "1.0.0"),
        (("description", "scene_authoring", "registry", "runtime", "agent"), {}),
    ],
)
def test_wrong_runtime_identity_never_dispatches_authoring(
    runtime_case, capsys, field, value
):
    case = runtime_case
    # Break shared fixture aliases, so changing describe cannot change E/metadata.
    case["description"] = copy.deepcopy(case["description"])
    case["metadata"] = copy.deepcopy(case["metadata"])
    target = case
    for part in field[:-1]:
        target = target[part]
    target[field[-1]] = value
    assert main(command(case)) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["issues"][0]["code"] == "runtime_identity_mismatch"
    assert report["local_physics"] == "not_run"
    assert not case["private"].exists()
    assert not any("scene-record-reference" in argv for _, argv, _ in case["calls"])


@pytest.mark.parametrize(
    "code", ["runtime_timeout", "runtime_output_invalid", "runtime_output_limit"]
)
def test_probe_preserves_safe_process_error_classification(
    runtime_case, monkeypatch, capsys, code
):
    def failed_probe(*args, **kwargs):
        raise ScenePackageError(code, "runtime", "Safe process diagnostic.")

    monkeypatch.setattr(runtime_case["bridge"], "_invoke", failed_probe)
    assert main(command(runtime_case)) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["issues"][0]["code"] == code
    assert result["local_physics"] == "not_run"


@pytest.mark.parametrize("target", ["private", "manifest", "output", "private_report"])
def test_existing_outputs_never_overwritten(runtime_case, capsys, target):
    case = runtime_case
    path = case[target]
    if target == "private":
        path.mkdir()
        path = path / "keep.json"
    path.write_text("original-private-marker")
    assert main(command(case)) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["local_physics"] == "not_run"
    assert not case["calls"]
    assert path.read_text() == "original-private-marker"
    assert "original-private-marker" not in json.dumps(report)


@pytest.mark.parametrize(
    "target", ["private", "manifest", "output", "private_report", "script"]
)
def test_nested_or_script_inputs_never_run(runtime_case, capsys, target):
    case = runtime_case
    case[target] = case["execution"] / "unsafe"
    if target == "script":
        case[target].write_text("not a public solver")
    assert main(command(case)) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["issues"][0]["code"] in {"output_layout_invalid", "script_path"}
    assert not case["calls"]


@pytest.mark.parametrize(
    "option,value",
    [
        ("--timeout-seconds", "nan"),
        ("--timeout-seconds", "0"),
        ("--timeout-seconds", "901"),
        ("--max-reference-steps", "501"),
        ("--max-reference-steps", "0"),
    ],
)
def test_local_caps_cannot_raise_contract_budget(runtime_case, capsys, option, value):
    args = command(runtime_case)
    args += [option, value]
    assert main(args) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["issues"][0]["code"] == "runtime_budget_invalid"
    assert not runtime_case["calls"]


def test_lower_recording_cap_is_forwarded(runtime_case, capsys):
    assert main([*command(runtime_case), "--max-reference-steps", "20"]) == 0
    capsys.readouterr()
    argv = next(
        argv for _, argv, _ in runtime_case["calls"] if "scene-record-reference" in argv
    )
    assert argv[argv.index("--max-reference-steps") + 1] == "20"


@pytest.mark.parametrize("reauthor_environment", [True, False])
def test_runtime_patch_needs_its_exact_new_environment_not_a_cli_version_guess(
    runtime_case, capsys, reauthor_environment
):
    case = runtime_case
    description = case["description"]["scene_authoring"]
    description["runtime_version"] = case["metadata"]["runtime_version"] = "0.3.1"
    registry = description["registry"]
    registry["runtime"]["version"] = "0.3.1"
    registry["version"] = "synthetic-runtime-031"
    environment = description["environment"]
    if reauthor_environment:
        environment.update(
            version=registry["version"], digest=canonical_digest(registry)
        )
    # A separately authored new E, never an in-command rewrite of the old E.
    (case["execution"] / "task.json").write_text(json.dumps(case["task"]))
    before = contents_digest(execution_input(case["execution"])[1])
    assert main(command(case)) == (0 if reauthor_environment else 1)
    result = json.loads(capsys.readouterr().out)
    if reauthor_environment:
        assert result["reference_ready"] is True
    else:
        assert result["issues"][0]["code"] == "runtime_identity_mismatch"
        assert result["local_physics"] == "not_run"
    assert contents_digest(execution_input(case["execution"])[1]) == before


def test_old_record_form_gets_migration_without_loading_private_root(
    runtime_case, capsys
):
    case = runtime_case
    shutil.rmtree(case["root"] / "private")
    assert (
        main(
            [
                "task",
                "scene",
                "record-reference",
                str(case["root"]),
                "--script",
                str(case["script"]),
                "--output",
                str(case["output"]),
            ]
        )
        == 1
    )
    report = json.loads(capsys.readouterr().out)
    assert report["issues"][0]["code"] == "scene_argument_migration"
    assert "NEW_P" in report["issues"][0]["message"]
    assert not case["calls"]


@pytest.mark.parametrize("change", ["private", "manifest", "expected"])
def test_self_test_drift_blocks_before_any_runtime(runtime_case, capsys, change):
    case = runtime_case
    case["private"] = case["root"] / "private"
    case["manifest"] = case["root"] / "submission.json"
    args = command(case, "self-test")
    if change == "private":
        (case["private"] / "private-extra").write_text("never-publish-this")
    elif change == "manifest":
        doc = json.loads(case["manifest"].read_text())
        doc["submission_digest"] = "0" * 64
        case["manifest"].write_text(json.dumps(doc))
    else:
        args += ["--expect-submission-digest", "0" * 64]
    assert main(args) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["local_physics"] == "not_run"
    assert not case["calls"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("execution_package_digest", "0" * 64),
        ("registry", {}),
        ("execution_contract", {}),
        ("execution_profile", {}),
        ("runtime_version", "unknown"),
        ("business_model_calls", 1),
        ("physical_checks_complete", False),
        ("checks", []),
        ("tool_reference_verified", False),
    ],
)
def test_unbound_or_static_receipt_cannot_claim_physics(
    runtime_case, capsys, field, value
):
    case = runtime_case
    case["private"] = case["root"] / "private"
    case["manifest"] = case["root"] / "submission.json"
    case["mutate"] = lambda body: body.update({field: value})
    assert main(command(case, "self-test")) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["issues"][0]["code"] == "runtime_report_invalid"
    assert report["local_physics"] == "incomplete"
    assert "never-publish-this" not in case["output"].read_text()


def test_record_receipt_requires_all_initial_states(runtime_case, capsys):
    runtime_case["mutate"] = lambda body: body.update(instances=0, steps={})
    assert main(command(runtime_case)) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["issues"][0]["code"] == "runtime_report_invalid"


@pytest.mark.parametrize("name", ["runtime.solvable", "runtime.noise", "runtime.nop"])
def test_self_test_receipt_cannot_skip_an_initial_state(runtime_case, capsys, name):
    case = runtime_case
    case["private"] = case["root"] / "private"
    case["manifest"] = case["root"] / "submission.json"

    def skip_state(body):
        next(c for c in body["checks"] if c["name"] == name)["detail"]["total"] = 1

    case["mutate"] = skip_state
    assert main(command(case, "self-test")) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["issues"][0]["code"] == "runtime_report_invalid"


def test_runtime_receipt_exit_and_selected_identity_must_agree(runtime_case, capsys):
    case = runtime_case
    case["accepted"] = False  # child exits 1, despite a successful record receipt
    assert main(command(case)) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["issues"][0]["code"] == "runtime_exit_mismatch"


def test_identity_drift_after_execution_rejects_receipt(runtime_case, capsys):
    case = runtime_case
    case["mutate"] = lambda _: case["metadata"].update(extra_identity="changed")
    assert main(command(case)) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["issues"][0]["code"] == "runtime_identity_drift"


@pytest.mark.parametrize(
    "code", ["reference_tool_proof_failed", "secret-private-error"]
)
def test_runtime_failure_codes_are_projected_not_echoed(runtime_case, capsys, code):
    case = runtime_case
    case["mutate"] = lambda body: body.update(
        ok=False, code=code, error="secret-private-error"
    )
    assert main(command(case)) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["issues"][0]["code"] == (
        code if code == "reference_tool_proof_failed" else "scene_runtime_failed"
    )
    assert "secret-private-error" not in case["output"].read_text()


@pytest.mark.parametrize("change", ["private", "manifest"])
def test_runtime_cannot_change_existing_self_test_binding(runtime_case, capsys, change):
    case = runtime_case
    case["private"] = case["root"] / "private"
    case["manifest"] = case["root"] / "submission.json"

    def mutate(_):
        if change == "private":
            (case["private"] / "stray").write_text("private-marker")
        else:
            case["manifest"].write_text("{}")

    case["mutate"] = mutate
    assert main(command(case, "self-test")) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["local_physics"] == "incomplete"
    assert report["issues"]


def test_runtime_and_execution_cannot_drift_during_authoring(runtime_case, capsys):
    case = runtime_case

    def mutate(_):
        (case["execution"] / "stray.json").write_text("private change")

    case["mutate"] = mutate
    assert main(command(case)) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["issues"][0]["code"] == "execution_drift"
    assert report["local_physics"] == "incomplete"


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"[]",
        b'{"ok":true,"ok":false}',
        b'{"ok":NaN}',
        b"{} private-marker",
        b"\xff",
    ],
)
def test_child_json_is_bounded_strict_and_never_echoed(runtime_case, raw):
    bridge = runtime_case["bridge"]
    with pytest.raises(ValueError, match="runtime_output_invalid"):
        bridge._decode(raw)


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_real_child_output_limit_and_timeout_are_not_physics(
    runtime_case, monkeypatch, stream
):
    case = runtime_case
    bridge = case["bridge"]
    monkeypatch.setattr(bridge, "MAX_CAPTURE_BYTES", 1024)
    invoke = case["real_invoke"]
    with pytest.raises(ValueError, match="runtime_output_limit"):
        invoke(
            sys.executable,
            ["-c", f"import sys; sys.{stream}.write('x'*1000000)"],
            timeout=2,
        )
    before = time.monotonic()
    with pytest.raises(ValueError, match="runtime_timeout"):
        invoke(sys.executable, ["-c", "import time; time.sleep(10)"], timeout=0.15)
    assert time.monotonic() - before < 3


def test_real_child_json_and_isolated_interpreter(runtime_case, monkeypatch, tmp_path):
    marker = tmp_path / "source-splice"
    marker.mkdir()
    monkeypatch.setenv("PYTHONPATH", str(marker))
    rc, value = runtime_case["real_invoke"](
        sys.executable,
        [
            "-c",
            "import sys,json; print(json.dumps({'paths':sys.path,"
            "'isolated':sys.flags.isolated})); sys.stderr.write('private-marker')",
        ],
        timeout=2,
    )
    assert rc == 0 and value["isolated"] == 1
    assert str(marker) not in value["paths"]
    assert "private-marker" not in str(value)


@pytest.mark.parametrize(
    "code",
    [
        "import os,time; os.close(1); os.close(2); time.sleep(10)",
        "import subprocess,sys; subprocess.Popen([sys.executable,'-c',"
        "'import time; time.sleep(10)'])",
    ],
)
def test_real_child_and_inherited_pipes_cannot_outlive_deadline(runtime_case, code):
    started = time.monotonic()
    with pytest.raises(ValueError, match="runtime_timeout"):
        runtime_case["real_invoke"](sys.executable, ["-c", code], timeout=0.3)
    assert time.monotonic() - started < 3


def test_unavailable_selected_interpreter_is_structured(runtime_case):
    with pytest.raises(ValueError, match="runtime_unavailable"):
        runtime_case["real_invoke"]("/not/an/interpreter", [], timeout=1)


def test_retained_default_diagnostics_are_private_and_self_test_shorthand_works(
    runtime_case, capsys
):
    case = runtime_case
    assert (
        main(
            [
                "task",
                "scene",
                "self-test",
                str(case["root"]),
                "--output",
                str(case["output"]),
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    diagnostic = Path(result["private_report"])
    assert diagnostic.exists()
    assert diagnostic.parent.stat().st_mode & 0o077 == 0
    assert case["output"].stat().st_mode & 0o077 == 0
    # Clean only the synthetic test directory, never an author's retained report.
    assert diagnostic.parent.name.startswith("openroboto-scene-private-")
    shutil.rmtree(diagnostic.parent)
