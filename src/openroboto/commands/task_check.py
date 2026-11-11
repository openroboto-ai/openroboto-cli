"""The task branch of check: build, record, and call worker admission."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

from openroboto.check_receipt import invalidate_receipt, write_receipt
from openroboto.config import ConfigError, Settings
from openroboto.console import say
from openroboto.task_package import (
    TaskPackageError,
    load_package,
    package_digest,
    validate_package,
)
from openroboto.task_runtime import TaskRuntime, resolve_runtime
from openroboto.task_workspace import json_text, workspace_path


def _has_reference(package: Path) -> bool:
    loaded = load_package(package)
    reference_path = loaded.task.get("reference_solution")
    if not reference_path or not (package / reference_path).is_file():
        return False
    reference = json.loads((package / reference_path).read_text())
    instances = reference.get("instances", {})
    return bool(instances) and all(item.get("actions") for item in instances.values())


def static_package(package: Path, settings: Settings) -> dict[str, Any]:
    from openroboto.commands.task import inspect

    summary = inspect(package)
    loaded = load_package(package)
    policy = settings.competition_params["task"]
    issues = validate_package(
        loaded, expected_execution_profile=policy["execution_profile"]
    )
    summary["issues"] = [
        {"code": i.code, "message": i.message, "severity": i.severity} for i in issues
    ]
    if loaded.source != policy["source"]:
        summary["issues"].append(
            {
                "code": "source_mismatch",
                "message": f"Expected source {policy['source']}, got {loaded.source}",
                "severity": "error",
            }
        )
    summary["ok"] = not any(i["severity"] == "error" for i in summary["issues"])
    return summary


def _definition(path: Path, profile: object) -> dict[str, Any]:
    from openroboto.commands.task_authoring import _definition as read_definition

    document = read_definition(str(path))
    if document["execution_profile"] != profile:
        raise ConfigError(
            f"{path}: execution_profile differs from this season. "
            "Use the profile in catalog.json."
        )
    return document


def run(args: argparse.Namespace, settings: Settings) -> int:
    result: dict[str, Any] = {"ok": False, "ready_to_submit": False, "checks": []}
    output: Path | None = None
    try:
        package = (
            Path(args.path).expanduser().resolve()
            if args.path
            else workspace_path(settings, args.config, "package")
        )
        invalidate_receipt(settings, args.config, package)
        if args.output:
            from openroboto.commands.task_authoring import _external_file

            package = (
                Path(args.path).expanduser().resolve()
                if args.path
                else workspace_path(settings, args.config, "package")
            )
            selected = _external_file(args.output, package, must_exist=False)
            if selected.exists():
                raise ConfigError(
                    "Choose a new output report; existing evidence is not overwritten."
                )
            output = selected
        result.update(_check(args, settings))
        if output is not None:
            output.write_text(json_text(result))
        written = write_receipt(
            settings,
            args.config,
            result,
            scope="static-only" if args.static_only else "full",
        )
        if written is not None:
            result["receipt"] = str(written)
    except (ConfigError, TaskPackageError, OSError, ValueError) as exc:
        result["ok"] = False
        result["ready_to_submit"] = False
        result["error"] = str(exc)
        result["next"] = (
            "Fix task.json / the reported package field, then run "
            "openroboto check again."
        )
    if args.json:
        say(json_text(result).rstrip())
    else:
        say(
            "READY TO SUBMIT"
            if result["ready_to_submit"]
            else "STATIC CHECK PASSED"
            if result["ok"]
            else "NOT READY"
        )
        if result.get("package_digest"):
            say(f"package_digest: {result['package_digest']}")
        for issue in result.get("issues", []):
            say(f"[{issue['code']}] {issue['message']}")
        for check in result["checks"]:
            if not check["passed"]:
                say(f"{check['name']}: {json.dumps(check['detail'], sort_keys=True)}")
        for key in ("error", "reason", "next", "report"):
            if result.get(key):
                say(f"{key}: {result[key]}")
    return 0 if result["ok"] else 1


def _check(args: argparse.Namespace, settings: Settings) -> dict[str, Any]:
    policy = settings.competition_params.get("task", {})
    profile = policy.get("execution_profile")
    definition = workspace_path(settings, args.config, "definition")
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
        raise ConfigError("Keep evidence/ and solve.py outside the task package.")
    definition_hash = ""
    if not args.path and definition.is_file():
        _definition(definition, profile)
        definition_hash = hashlib.sha256(definition.read_bytes()).hexdigest()
    build_record = evidence / "build.json"
    previous = json.loads(build_record.read_text()) if build_record.is_file() else {}
    rebuild = not package.is_dir() or bool(
        definition_hash and previous.get("definition_sha256") != definition_hash
    )
    summary: dict[str, Any] = {"ok": True, "checks": []}
    if not rebuild:
        summary = static_package(package, settings)
        errors = [i for i in summary["issues"] if i["severity"] == "error"]
        recordable = {"axis.reference_actions_missing", "axis.reference_missing"}
        if any(i["code"] not in recordable for i in errors):
            return {
                **summary,
                "ready_to_submit": False,
                "next": "Fix the reported fields, then run openroboto check again.",
            }
    elif not definition_hash:
        raise ConfigError(
            f"No task package at {package}. "
            f"Create {definition} using openroboto create, "
            "or pass an existing package path."
        )
    if args.static_only:
        return {
            **summary,
            "ready_to_submit": False,
            "phase": "static",
            "next": "Run openroboto check for worker static and physical "
            "admission before submitting.",
        }
    release = resolve_runtime(settings)
    missing_reference = rebuild or not _has_reference(package)
    if args.repo and missing_reference:
        return {
            "ok": False,
            "ready_to_submit": False,
            "checks": [],
            "error": "The pinned remote package has no complete reference actions.",
            "next": (
                "Record a reference locally, upload a new commit, "
                "then check that commit."
            ),
        }
    if missing_reference and not args.example and not solve.is_file():
        return {
            "ok": False,
            "ready_to_submit": False,
            "checks": [],
            "error": f"Missing reference solution. Write {solve} with "
            "solve(runtime, task, instance), or use openroboto check "
            "--example for the v1 one-cube example.",
            "next": f"Add {solve}, then run openroboto check.",
        }
    runtime = TaskRuntime(settings)
    runtime.prepare()
    evidence.mkdir(parents=True, exist_ok=True)
    attempt = Path(tempfile.mkdtemp(prefix="check-", dir=evidence))
    work = attempt / "package"
    if rebuild:
        built = runtime.run(
            ["build", "--definition", str(definition), "--output", str(work)],
            [(definition, False), (attempt, True)],
        )
        if built.get("accepted") is False:
            return {
                **built,
                "ok": False,
                "ready_to_submit": False,
                "next": "Fix task.json using catalog.json, then run openroboto check.",
            }
    else:
        shutil.copytree(package, work)
    before = package_digest(work)
    if missing_reference:
        command = ["record-reference", str(work)]
        mounts = [(work, True)]
        if args.example:
            command.append("--example")
        else:
            command.extend(["--script", str(solve)])
            mounts.append((solve, False))
        recorded = runtime.run(command, mounts, timeout=900)
        (attempt / "record.json").write_text(json_text(recorded))
        if recorded.get("accepted") is False:
            return {
                **recorded,
                "ok": False,
                "ready_to_submit": False,
                "next": f"Fix {solve}; the reference must solve every instance. "
                "Run openroboto check again.",
                "report": str(attempt / "record.json"),
            }
    after = package_digest(work)
    if args.expect_digest and args.expect_digest.lower() != after:
        raise ConfigError(
            f"Expected digest {args.expect_digest} differs from {after}. "
            "Check the selected package and recorded reference."
        )
    arguments = [
        str(work),
        "--execution-profile",
        json.dumps(profile),
        "--source",
        policy["source"],
    ]
    static = runtime.run(["admit-static", *arguments], [(work, False)])
    (attempt / "static.json").write_text(json_text(static))
    if not static["accepted"]:
        verdict = static
    else:
        verdict = runtime.run(["self-test", *arguments], [(work, False)], timeout=180)
    if package_digest(work) != after:
        raise ConfigError(
            "Read-only admission changed package contents; no ready verdict is issued."
        )
    verdict.update(
        {
            "ok": verdict["accepted"],
            "ready_to_submit": verdict["accepted"],
            "image_digest": runtime.image,
            "runtime_content_sha256": release["content_sha256"],
            "digest_before_reference": before,
            "package_digest": after,
            "next": "Run openroboto submit."
            if verdict["accepted"]
            else f"Fix the failed checker/reference in {solve}, "
            "then run openroboto check again.",
            "report": str(attempt / "verdict.json"),
        }
    )
    (attempt / "verdict.json").write_text(json_text(verdict))
    if (rebuild or missing_reference) and not args.repo:
        # Recording is done in a working copy; publish the complete new bundle.
        if package.exists():
            package.rename(attempt / "previous-package")
        package.parent.mkdir(parents=True, exist_ok=True)
        work.rename(package)
        build_record.write_text(
            json_text({"definition_sha256": definition_hash, "package_digest": after})
        )
    (evidence / "check.json").write_text(json_text(verdict))
    return verdict


def run_structural(args: argparse.Namespace, settings: Settings) -> int:
    """Expose the raw worker structural stage for contract regression only."""
    result: dict[str, Any] = {
        "ok": False,
        "accepted": False,
        "code": None,
        "checks": [],
        "scope": "structural-only",
        "ready_to_submit": False,
    }
    try:
        package = (
            Path(args.path).expanduser().resolve()
            if args.path
            else workspace_path(settings, args.config, "package")
        )
        invalidate_receipt(settings, args.config, package)
        policy = settings.competition_params["task"]
        runtime = TaskRuntime(settings)
        runtime.prepare()
        verdict = runtime.run(
            [
                "admit-static",
                str(package),
                "--execution-profile",
                json.dumps(policy.get("execution_profile")),
                "--source",
                policy["source"],
            ],
            [(package, False)],
        )
        result.update(verdict)
        result.update(
            ok=verdict["accepted"], scope="structural-only", ready_to_submit=False
        )
    except (ConfigError, TaskPackageError, OSError, ValueError) as exc:
        result["error"] = str(exc)
    if args.json:
        say(json_text(result).rstrip())
    else:
        say(
            "STRUCTURAL ADMISSION PASSED"
            if result["ok"]
            else "STRUCTURAL ADMISSION FAILED"
        )
        say(
            "Full runtime admission has not been run. "
            "Run openroboto check before submitting."
        )
    return 0 if result["ok"] else 1
