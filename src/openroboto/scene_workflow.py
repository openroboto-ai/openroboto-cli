"""Workspace scene checks through the bundled image and strict authoring bridge."""

from __future__ import annotations

import argparse
import tempfile
from importlib.resources import files
from pathlib import Path
from typing import Any

from openroboto_protocol.scene_contract import profile_catalog
from openroboto_protocol.scene_schema import SceneTask

from openroboto.config import ConfigError, Settings
from openroboto.scene_authoring import run_authoring
from openroboto.scene_instruction import run_instruction
from openroboto.scene_package import (
    _read_regular,
    contents_digest,
    execution_input,
    load_submission,
)
from openroboto.task_runtime import TaskRuntime
from openroboto.task_workspace import json_text, workspace_path


def is_scene(settings: Settings) -> bool:
    return bool(
        settings.competition_params.get("task", {})
        .get("execution_profile", {})
        .get("id")
        == "panda-open-scene-v1"
    )


def starter_files() -> dict[str, str]:
    root = files("openroboto").joinpath("templates/scene")
    return {
        "task.json": root.joinpath("task.json").read_text(),
        "scene.xml": root.joinpath("scene.xml").read_text(),
        "assets/panel.obj": root.joinpath("assets/panel.obj").read_text(),
    }


def source_execution(
    definition: Path, output: Path
) -> tuple[SceneTask, dict[str, bytes]]:
    """Snapshot only declared dependencies; never copy workspace keys or solve.py."""
    limit = max(p["max_package_bytes"] for p in profile_catalog()["resources"].values())
    document = _read_regular(definition, limit=limit)
    task = SceneTask.model_validate_json(document)
    content = {"task.json": document}
    total = len(document)
    for dependency in task.scene.dependencies:
        source = definition.parent / dependency.path
        if not source.resolve().is_relative_to(definition.parent.resolve()):
            raise ConfigError(
                "Scene dependencies must stay within the definition directory."
            )
        data = _read_regular(source, limit=limit - total)
        total += len(data)
        content[dependency.path] = data
    output.mkdir(parents=True, mode=0o700)
    for name, data in content.items():
        path = output / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return execution_input(output)


def check(args: argparse.Namespace, settings: Settings) -> dict[str, Any]:
    package = (
        Path(args.path).expanduser().resolve()
        if args.path
        else workspace_path(settings, args.config, "package")
    )
    evidence = workspace_path(settings, args.config, "evidence")
    solve = (
        Path(args.script).expanduser().resolve()
        if args.script
        else workspace_path(settings, args.config, "solve")
    )
    if evidence.is_relative_to(package) or solve.is_relative_to(package):
        raise ConfigError("Keep evidence/ and solve.py outside the scene submission.")
    evidence.mkdir(parents=True, exist_ok=True)
    attempt = Path(tempfile.mkdtemp(prefix="scene-check-", dir=evidence))
    source = attempt / "source"
    if args.path:
        source = package / "execution" if (package / "execution").is_dir() else package
        task, content = execution_input(source)
    else:
        task, content = source_execution(
            workspace_path(settings, args.config, "definition"), source
        )
    policy = settings.competition_params["task"]
    if (
        task.execution_profile.model_dump() != policy["execution_profile"]
        or task.source != policy["source"]
    ):
        raise ConfigError(
            "Scene source or execution_profile differs from the season. "
            "Use catalog.json."
        )
    if "execution_contract" in settings.authoring:
        from openroboto_protocol.scene_contract import parse_execution_contract

        if parse_execution_contract(
            task.execution_contract
        ) != parse_execution_contract(settings.authoring["execution_contract"]):
            raise ConfigError(
                "Scene execution_contract differs from the season snapshot. "
                "Use the contract in catalog.json and record a fresh reference."
            )
    structural = {"name": "scene.structure", "passed": True, "kind": "structural"}
    result: dict[str, Any] = {
        "ok": True,
        "ready_to_submit": False,
        "checks": [structural],
        "package_digest": contents_digest(content),
        "server_admission": "not_run",
        "reward_status": "not_evaluated",
    }
    if args.path and source != package:
        load_submission(package)
    if args.static_only:
        result.update(
            phase="static",
            next="Run openroboto check to record and replay every initial "
            "state in the official container.",
        )
        return result
    if args.example:
        raise ConfigError(
            "--example supports only the v1 one-cube task. Write "
            "solve.py for this scene."
        )
    runtime = TaskRuntime(settings)
    runtime.prepare()
    candidate = attempt / "candidate"
    candidate.mkdir()
    if not args.path:
        with runtime.python_bridge([(attempt, True), (source, False)]) as python:
            rendering = run_instruction(
                argparse.Namespace(
                    execution=source,
                    output=attempt / "instruction.json",
                    prepared_execution=candidate / "execution",
                    runtime_python=python,
                    timeout_seconds=60.0,
                ),
                runtime_workdir=attempt,
            )
        if not rendering["ok"]:
            return {
                **result,
                "ok": False,
                "issues": rendering["issues"],
                "report": str(attempt / "instruction.json"),
            }
        execution = candidate / "execution"
    else:
        execution = source
    task, content = execution_input(execution)
    digest = contents_digest(content)
    reusable = (
        not getattr(args, "record_reference", False)
        and (package / "submission.json").is_file()
    )
    if reusable:
        original = load_submission(package)
        reusable = original.manifest.execution_package_digest == digest
    if reusable:
        selected = package
    else:
        if args.path or args.repo:
            raise ConfigError(
                "The selected scene has no matching bound reference. Record "
                "it locally, then upload a new commit and check again."
            )
        if not solve.is_file():
            raise ConfigError(
                f"Missing reference solution. Write {solve} with "
                "solve(runtime, task, instance), then run openroboto check."
            )
        inputs = settings.authoring.get("solve_inputs", [])
        if not isinstance(inputs, list) or not all(isinstance(p, str) for p in inputs):
            raise ConfigError(
                "authoring.solve_inputs must list explicit local data files."
            )
        mounts = [(attempt, True), (execution, False), (solve, False)]
        for name in inputs:
            path = (
                Path(args.config).resolve().parent / Path(name).expanduser()
            ).absolute()
            if path.resolve() == Path(args.config).resolve() or ".secrets" in path.name:
                raise ConfigError("Do not mount workspace credentials as solve inputs.")
            _read_regular(path, limit=8_388_608)
            mounts.append((path, False))
        with runtime.python_bridge(mounts) as python:
            recorded = run_authoring(
                argparse.Namespace(
                    scene_command="record-reference",
                    execution=execution,
                    private=candidate / "private",
                    manifest=candidate / "submission.json",
                    package=None,
                    output=attempt / "record.json",
                    private_report=attempt / "record-private.json",
                    runtime_python=python,
                    timeout_seconds=900.0,
                    script=solve,
                    max_reference_steps=None,
                    expect_submission_digest="",
                )
            )
        if not recorded["ok"]:
            return {
                **result,
                "ok": False,
                "issues": recorded["issues"],
                "report": str(attempt / "record.json"),
                "next": f"Fix {solve}; the reference must solve every initial state. "
                "Run openroboto check again.",
            }
        selected = candidate
    before = load_submission(selected)
    if (
        args.expect_digest
        and args.expect_digest.lower() != before.manifest.execution_package_digest
    ):
        raise ConfigError(
            "Expected package digest differs from the final scene execution root."
        )
    reports = attempt / "self-test"
    reports.mkdir(mode=0o700)
    with runtime.python_bridge([(selected, False), (reports, True)]) as python:
        verdict = run_authoring(
            argparse.Namespace(
                scene_command="self-test",
                package=selected,
                execution=None,
                private=None,
                manifest=None,
                output=reports / "public.json",
                private_report=reports / "private.json",
                runtime_python=python,
                timeout_seconds=180.0,
                expect_submission_digest=before.manifest.submission_digest,
            )
        )
    if load_submission(selected).manifest != before.manifest:
        raise ConfigError(
            "Read-only self-test changed the scene; no receipt was issued."
        )
    result.update(
        ok=verdict["ok"],
        ready_to_submit=verdict["ok"],
        package_digest=before.manifest.execution_package_digest,
        submission_digest=before.manifest.submission_digest,
        private_package_digest=before.manifest.private_package_digest,
        environment=task.environment.model_dump(),
        execution_contract_digest=verdict.get("execution_contract_digest"),
        runtime_identity_digest=verdict.get("runtime_identity_digest"),
        runtime_version=runtime.release["version"],
        admission_version=runtime.release["admission_version"],
        admission_policy_sha256=runtime.release["admission_policy_sha256"],
        runtime_content_sha256=runtime.release["content_sha256"],
        local_physics=verdict["local_physics"],
        image_digest=runtime.image,
        checks=[
            structural,
            *[{**row, "kind": "runtime"} for row in verdict.get("checks", [])],
        ],
        issues=verdict["issues"],
        report=str(reports / "public.json"),
        next="Run openroboto submit."
        if verdict["ok"]
        else "Inspect the private diagnostics, fix the checker/reference "
        "and run openroboto check again.",
    )
    if selected == candidate:
        if package.exists():
            package.rename(attempt / "previous-package")
        package.parent.mkdir(parents=True, exist_ok=True)
        candidate.rename(package)
    (evidence / "check.json").write_text(json_text(result))
    return result
