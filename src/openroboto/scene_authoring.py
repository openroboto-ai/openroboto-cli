"""Local scene authoring through a verified, separately installed runtime.

No simulator or author code runs in this process. Runtime reports/evidence are
private files; the CLI projects only identities, fixed errors and check booleans.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import pathlib
import selectors
import shutil
import signal
import subprocess
import tempfile
import time
from typing import Any, NoReturn

from openroboto_protocol.scene_contract import (
    parse_execution_contract,
    profile_catalog,
    profile_ref,
)
from openroboto_protocol.scene_schema import SceneTask, SubmissionManifest
from openroboto_protocol.tabletop_assets import canonical_digest

from openroboto.scene_package import (
    ScenePackageError,
    _read_regular,
    contents_digest,
    execution_input,
    load_bound_roots,
)

MAX_CAPTURE_BYTES = 1_048_576
MAX_REPORT_BYTES = 8_388_608
MAX_TIMEOUT_SECONDS = 900
# Probe the installed distributions and schema, not a version supplied by E.
IDENTITY_PROBE = (
    "import json; from importlib.metadata import version; "
    "from openroboto_protocol.scene_contract import profile_catalog; "
    "from task_source.scene_runtime import registry_refs; "
    "print(json.dumps({'protocol_version':version('openroboto-protocol'),"
    "'runtime_version':version('openroboto-task-runtime'),"
    "'profile':profile_catalog(),'refs':registry_refs()}))"
)
CHECK_NAMES = frozenset(
    {
        "runtime.solvable",
        "runtime.deterministic",
        "runtime.noise",
        "runtime.nop",
        "runtime.visibility",
    }
)
ERROR_MESSAGES = {
    "scene_observation_policy_scope_unregistered": (
        "Observation policy scope is not registered for this profile. "
        "Use a newly registered matching runtime; retain E/P unchanged."
    ),
    "runtime.timeout": "The local deadline expired; inspect private diagnostics.",
    "scene_runtime_unavailable": "Install the matching reviewed CPU runtime.",
    "reference_step_budget": "The reference exceeded its lower local step cap.",
    "reference_tool_budget": "The reference exceeded the frozen tool-call budget.",
    "reference_tool_proof_failed": "Tool verification failed; inspect private proof.",
    "scene_recording_failed": "The solver failed; inspect private diagnostics.",
    "scene_static_checks_failed": "Runtime structure checks failed; inspect privately.",
    "motion_mismatch": (
        "Native object motion differs from the declared scene joints. "
        "Correct task_definition and its checker in a NEW E; retain the "
        "private diagnostics, then run openroboto check again."
    ),
    "reference_instances_failed": (
        "The reference did not complete every initial state. Inspect the "
        "private per-instance diagnostics, fix solve.py or the authored scene, "
        "and run openroboto check again."
    ),
}


def _fail(code: str, message: str) -> NoReturn:
    raise ScenePackageError(code, "runtime", message)


def _decode(data: bytes) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in items:
            if key in value:
                raise ValueError("duplicate key")
            value[key] = item
        return value

    def constant(_: str) -> None:
        raise ValueError("nonfinite JSON")

    def finite_float(token: str) -> float:
        value = float(token)
        if not math.isfinite(value):
            raise ValueError("nonfinite JSON number")
        return value

    try:
        value = json.loads(
            data,
            object_pairs_hook=pairs,
            parse_constant=constant,
            parse_float=finite_float,
        )
        if not isinstance(value, dict):
            raise ValueError("object required")
        return value
    except (ValueError, RecursionError) as exc:
        raise ScenePackageError(
            "runtime_output_invalid",
            "runtime",
            "Install a compatible runtime or inspect private diagnostics; "
            "expected one bounded JSON object.",
        ) from exc


def _invoke(
    python: str, argv: list[str], *, timeout: float
) -> tuple[int, dict[str, Any]]:
    """Bound both pipes and the process group; never relay raw child output."""
    try:
        process = subprocess.Popen(
            [python, "-I", *argv],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as exc:
        raise ScenePackageError(
            "runtime_unavailable", "runtime", "Choose an installed --runtime-python."
        ) from exc
    output = bytearray()
    size = 0
    deadline = time.monotonic() + timeout
    try:
        with selectors.DefaultSelector() as selector:
            for stream in (process.stdout, process.stderr):
                assert stream is not None
                selector.register(stream, selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    _fail(
                        "runtime_timeout",
                        "Local deadline expired; retain failed files.",
                    )
                for key, _ in selector.select(min(remaining, 0.1)):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    size += len(chunk)
                    if size > MAX_CAPTURE_BYTES:
                        _fail("runtime_output_limit", "Runtime output exceeded 1 MiB.")
                    if key.fileobj is process.stdout:
                        output.extend(chunk)
            try:
                returncode = process.wait(
                    timeout=max(0.001, deadline - time.monotonic())
                )
            except subprocess.TimeoutExpired:
                _fail("runtime_timeout", "Local deadline expired; retain failed files.")
        return returncode, _decode(bytes(output))
    finally:
        # A solver may have children holding a pipe open after its parent exits.
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        elif process.poll() is None:  # pragma: no cover - POSIX CI/author hosts
            process.kill()
        process.wait()
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()


def _probe(python: str, task: SceneTask, deadline: float) -> dict[str, Any]:
    """Bind capabilities to installed versions, the full registry and E's agent."""
    from importlib.metadata import version

    try:
        status, metadata = _invoke(
            python, ["-c", IDENTITY_PROBE], timeout=_remaining(deadline, maximum=10)
        )
        rc, described = _invoke(
            python,
            ["-m", "task_source.authoring", "describe", "--include-development"],
            timeout=_remaining(deadline, maximum=10),
        )
        description = described["scene_authoring"]
        registry = description["registry"]
        runtime = registry["runtime"]
        refs = metadata["refs"]
        contract = parse_execution_contract(task.execution_contract)
        commands = description["commands"]
        valid = (
            status == rc == 0
            and isinstance(description, dict)
            and isinstance(registry, dict)
            and isinstance(runtime, dict)
            and isinstance(refs, dict)
            and metadata["protocol_version"] == version("openroboto-protocol")
            and isinstance(metadata["runtime_version"], str)
            and bool(metadata["runtime_version"])
            and metadata["runtime_version"]
            == description["runtime_version"]
            == runtime["version"]
            and isinstance(metadata["profile"], dict)
            and canonical_digest(metadata["profile"])
            == canonical_digest(profile_catalog())
            and type(metadata["profile"]["task_schema_version"]) is int
            and metadata["profile"]["task_schema_version"] == task.schema_version == 3
            and type(metadata["profile"]["execution_contract_version"]) is int
            and metadata["profile"]["execution_contract_version"]
            == contract["version"]
            == 3
            and type(description["api_version"]) is int
            and description["api_version"] == 1
            and description["status"] == "development"
            and isinstance(commands, list)
            and all(isinstance(command, str) for command in commands)
            and len(set(commands)) == len(commands)
            and set(commands) >= {"scene-record-reference", "scene-self-test"}
            and runtime["protocol_version"] == metadata["protocol_version"]
            and description["execution_profile"]
            == runtime["execution_profile"]
            == profile_ref()
            and description["environment"]
            == refs["environment"]
            == task.environment.model_dump()
            and refs["environment"]
            == {
                "id": registry["id"],
                "version": registry["version"],
                "digest": canonical_digest(registry),
            }
            and refs["artifact"] == task.robot.artifact.model_dump()
            and refs["control_mapping"] == task.robot.control_mapping.model_dump()
            and runtime["agent"] == contract["agent"]
        )
        if not valid:
            raise ValueError("identity mismatch")
    except ScenePackageError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise ScenePackageError(
            "runtime_identity_mismatch",
            "runtime",
            "Use scene API v1 with this exact protocol, schema/profile, environment "
            "and agent. A changed runtime needs a newly authored E; "
            "do not rewrite E here.",
        ) from exc
    return {"metadata": metadata, "description": description}


def _remaining(deadline: float, *, maximum: float = MAX_TIMEOUT_SECONDS) -> float:
    seconds = min(maximum, deadline - time.monotonic())
    if seconds <= 0:
        _fail("runtime_timeout", "The total local authoring deadline expired.")
    return seconds


def _new(path: pathlib.Path) -> None:
    if path.exists() or path.is_symlink():
        raise ScenePackageError(
            "output_exists", "output", "Choose new private/manifest/report paths."
        )


def _layout(paths: list[pathlib.Path]) -> None:
    for i, path in enumerate(paths):
        for other in paths[i + 1 :]:
            a, b = path.resolve(), other.resolve()
            if a == b or a.is_relative_to(b) or b.is_relative_to(a):
                raise ScenePackageError(
                    "output_layout_invalid",
                    "output",
                    "Keep E, P, manifest, reports and evidence disjoint.",
                )


def _result(
    body: dict[str, Any],
    task: SceneTask,
    identity: dict[str, Any],
    digest: str,
    manifest: SubmissionManifest,
    *,
    recording: bool,
) -> dict[str, Any]:
    """Check a private receipt's binding; physics decisions remain runtime-owned."""
    try:
        contract = parse_execution_contract(task.execution_contract)
        if not (
            body["ok"] is True
            and body["capability"] == "local_physics"
            and body["source"] == "native_mujoco_local_cpu"
            and type(body["business_model_calls"]) is int
            and body["business_model_calls"] == 0
            and body["runtime_version"] == identity["metadata"]["runtime_version"]
            and body["execution_profile"] == profile_ref()
            and body["environment"] == task.environment.model_dump()
            and isinstance(body["registry"], dict)
            and canonical_digest(body["registry"])
            == canonical_digest(identity["description"]["registry"])
            and isinstance(body["execution_contract"], dict)
            and canonical_digest(body["execution_contract"])
            == canonical_digest(contract)
            and body["execution_package_digest"] == digest
            and manifest.execution_package_digest == digest
        ):
            raise ValueError("report binding")
        total = len(task.scene.initial_states)
        if recording:
            steps = body["steps"]
            if not (
                body["reference_ready"] is True
                and type(body["instances"]) is int
                and body["instances"] == total
                and isinstance(steps, dict)
                and set(steps) == {str(i) for i in range(total)}
                and all(
                    type(n) is int and 0 < n <= contract["budget"]["max_steps"]
                    for n in steps.values()
                )
                and isinstance(body["submission"], dict)
                and canonical_digest(body["submission"])
                == canonical_digest(manifest.model_dump(mode="json"))
            ):
                raise ValueError("reference coverage")
            return {
                "ok": True,
                "reference_ready": True,
                "instances": total,
                "local_physics": "reference_recorded",
            }
        rows = body["checks"]
        if not (
            isinstance(rows, list)
            and all(
                isinstance(item, dict)
                and isinstance(item.get("name"), str)
                and type(item.get("passed")) is bool
                and isinstance(item.get("detail"), dict)
                for item in rows
            )
        ):
            raise ValueError("check shapes")
        checks = {item["name"]: item for item in rows}
        if not (
            body["physical_checks_complete"] is True
            and type(body["accepted"]) is bool
            and type(body["tool_reference_verified"]) is bool
            and len(checks) == len(body["checks"])
            and CHECK_NAMES <= checks.keys()
            and all(type(item["passed"]) is bool for item in checks.values())
            and type(checks["runtime.solvable"]["detail"]["total"]) is int
            and checks["runtime.solvable"]["detail"]["total"] == total
            and type(checks["runtime.nop"]["detail"]["total"]) is int
            and checks["runtime.nop"]["detail"]["total"] == total
            and type(checks["runtime.noise"]["detail"]["trials"]) is int
            and checks["runtime.noise"]["detail"]["trials"] > 0
            and type(checks["runtime.noise"]["detail"]["total"]) is int
            and checks["runtime.noise"]["detail"]["total"]
            == total * checks["runtime.noise"]["detail"]["trials"]
            and body["accepted"]
            == (
                all(c["passed"] for c in checks.values())
                and body["tool_reference_verified"]
            )
        ):
            raise ValueError("incomplete physics receipt")
        return {
            "ok": body["accepted"],
            "accepted": body["accepted"],
            "physical_checks_complete": True,
            "instances": total,
            "checks": [
                {"name": n, "passed": checks[n]["passed"]} for n in sorted(CHECK_NAMES)
            ],
            "local_physics": "passed" if body["accepted"] else "failed",
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise ScenePackageError(
            "runtime_report_invalid",
            "runtime",
            "No complete bound receipt; inspect private diagnostics and retry.",
        ) from exc


def run_authoring(args: argparse.Namespace) -> dict[str, Any]:
    """Run explicit all-state scene commands; preserve E and existing P/manifest."""
    report: dict[str, Any] = {
        "ok": False,
        "scope": "local_authoring",
        "capability": "resource_unavailable",
        "local_physics": "not_run",
        "server_admission": "not_run",
        "reward_status": "not_evaluated",
        "issues": [],
    }
    public = args.output.expanduser().absolute()
    writable = False
    try:
        if public.exists() or public.is_symlink():
            _fail("report_path", "Choose a new public report outside both roots.")
        recording = args.scene_command == "record-reference"
        if args.package and (
            recording or args.execution or args.private or args.manifest
        ):
            _fail(
                "scene_argument_migration",
                "Record using --execution E --private NEW_P "
                "--manifest NEW_JSON --script SCRIPT --output PUBLIC_JSON. "
                "For self-test "
                "use either a complete package or explicit E/P/manifest, not both.",
            )
        if args.package:
            package = pathlib.Path(args.package).expanduser().absolute()
            execution, private, manifest = (
                package / "execution",
                package / "private",
                package / "submission.json",
            )
            if public.resolve().is_relative_to(package.resolve()):
                _fail("report_path", "Keep reports outside the submission tree.")
        elif args.execution and args.private and args.manifest:
            execution, private, manifest = (
                p.expanduser().absolute()
                for p in (args.execution, args.private, args.manifest)
            )
        else:
            _fail(
                "scene_arguments_required",
                "Supply --execution E --private P --manifest JSON; "
                "recording requires NEW P and manifest. "
                "Self-test also accepts a complete package.",
            )
        if args.private_report:
            diagnostic = args.private_report.expanduser().absolute()
        else:
            diagnostic = (
                pathlib.Path(tempfile.mkdtemp(prefix="openroboto-scene-private-"))
                / "report.json"
            )
        evidence = diagnostic.with_suffix(diagnostic.suffix + ".evidence")
        _layout([execution, private, manifest, public, diagnostic, evidence])
        _new(diagnostic)
        _new(evidence)
        report["private_report"] = str(diagnostic)
        if not public.parent.is_dir():
            _fail("report_path", "Create the public report's parent directory first.")
        bundle = execution.parent
        if (
            execution.name == "execution"
            and private == bundle / "private"
            and manifest == bundle / "submission.json"
            and any(
                p.resolve().is_relative_to(bundle.resolve())
                for p in (public, diagnostic)
            )
        ):
            _fail("report_path", "Keep reports outside the whole submission tree.")
        writable = True
        if recording:
            _new(private)
            _new(manifest)
        task, files = execution_input(execution)
        digest = contents_digest(files)
        bound = None
        if not recording:
            task, bound = load_bound_roots(execution, private, manifest)
            if bound.execution_package_digest != digest:
                _fail("execution_drift", "E changed while binding the input.")
            if (
                args.expect_submission_digest
                and bound.submission_digest != args.expect_submission_digest
            ):
                _fail("submission_drift", "Use the exact expected E/P/joint identity.")
        if (
            not math.isfinite(args.timeout_seconds)
            or not 0 < args.timeout_seconds <= MAX_TIMEOUT_SECONDS
        ):
            _fail(
                "runtime_budget_invalid",
                "Choose a local deadline greater than zero and at most 900 seconds.",
            )
        contract = parse_execution_contract(task.execution_contract)
        step_cap = getattr(args, "max_reference_steps", None)
        if step_cap is not None and not 0 < step_cap <= contract["budget"]["max_steps"]:
            _fail(
                "runtime_budget_invalid",
                "--max-reference-steps may only lower the frozen step budget.",
            )
        script = getattr(args, "script", None)
        if recording:
            assert isinstance(script, pathlib.Path)
            script = script.expanduser().absolute()
            if (
                script.is_symlink()
                or not script.is_file()
                or any(
                    script.resolve().is_relative_to(p.resolve())
                    for p in (execution, private)
                )
            ):
                _fail("script_path", "Select a regular local solver outside E and P.")
        python = shutil.which(str(pathlib.Path(args.runtime_python).expanduser()))
        if python is None:
            _fail("runtime_unavailable", "Choose an installed --runtime-python.")
        deadline = time.monotonic() + args.timeout_seconds
        identity = _probe(python, task, deadline)
        description = identity["description"]
        if "observation_policy_scope_matches_profile" in description:
            scope = description["observation_policy_scope_matches_profile"]
            if type(scope) is not bool:
                _fail(
                    "runtime_identity_mismatch",
                    "Expected an explicit boolean policy scope.",
                )
            if not scope:
                scope_code = "scene_observation_policy_scope_unregistered"
                _fail(scope_code, ERROR_MESSAGES[scope_code])
        argv = [
            "-m",
            "task_source.authoring",
            "scene-record-reference" if recording else "scene-self-test",
            "--execution",
            str(execution),
            "--private",
            str(private),
            "--manifest",
            str(manifest),
            "--output",
            str(diagnostic),
            "--timeout-seconds",
            str(args.timeout_seconds),
        ]
        if recording:
            argv += ["--script", str(script)]
            if step_cap is not None:
                argv += ["--max-reference-steps", str(step_cap)]
        else:
            assert bound is not None
            argv += ["--expect-submission-digest", bound.submission_digest]
        diagnostic.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Recheck after describe, before any author code or physics is dispatched.
        if contents_digest(execution_input(execution)[1]) != digest:
            _fail(
                "execution_drift",
                "E changed before execution; retain the original input.",
            )
        if recording:
            _new(private)
            _new(manifest)
        else:
            if load_bound_roots(execution, private, manifest)[1] != bound:
                _fail("submission_drift", "P/manifest changed before execution.")
        report["local_physics"] = "incomplete"
        try:
            rc, _ = _invoke(python, argv, timeout=_remaining(deadline))
        finally:
            if contents_digest(execution_input(execution)[1]) != digest:
                _fail(
                    "execution_drift",
                    "Runtime changed E; retain the failure, do not submit.",
                )
            if (
                not recording
                and load_bound_roots(execution, private, manifest)[1] != bound
            ):
                _fail("submission_drift", "Runtime changed P/manifest; do not submit.")
        # Snapshot the receipt and its binding before the post-execution probe.
        # The probe must not turn a check of A into an accepted receipt for B.
        body = _decode(_read_regular(diagnostic, limit=MAX_REPORT_BYTES))
        result_manifest = bound
        if recording and body.get("ok") is True:
            _, result_manifest = load_bound_roots(execution, private, manifest)
        if canonical_digest(_probe(python, task, deadline)) != canonical_digest(
            identity
        ):
            _fail(
                "runtime_identity_drift",
                "The selected runtime changed during execution.",
            )
        final_task, final_files = execution_input(execution)
        if contents_digest(final_files) != digest or canonical_digest(
            parse_execution_contract(final_task.execution_contract)
        ) != canonical_digest(contract):
            _fail(
                "execution_drift", "E/contract changed during the final runtime probe."
            )
        if result_manifest is not None:
            _, final_manifest = load_bound_roots(execution, private, manifest)
            if final_manifest != result_manifest or (
                not recording
                and args.expect_submission_digest
                and final_manifest.submission_digest != args.expect_submission_digest
            ):
                _fail("submission_drift", "P/manifest changed during the final probe.")
        if body.get("ok") is not True:
            if body.get("capability") in ("invalid_package", "unsupported", "failed"):
                report["capability"] = body["capability"]
            code = body.get("code")
            message = ERROR_MESSAGES.get(code) if isinstance(code, str) else None
            _fail(
                code if isinstance(code, str) and message else "scene_runtime_failed",
                message
                or "Inspect the private runtime report; no completed physics receipt.",
            )
        assert result_manifest is not None
        result = _result(
            body, task, identity, digest, result_manifest, recording=recording
        )
        if (
            recording
            and step_cap is not None
            and any(n > step_cap for n in body["steps"].values())
        ):
            _fail("runtime_report_invalid", "Reference exceeded the lower local cap.")
        if (rc == 0) != result["ok"]:
            _fail(
                "runtime_exit_mismatch",
                "Runtime exit code disagrees with its private receipt.",
            )
        report.update(
            result,
            capability="local_physics",
            submission=result_manifest.model_dump(mode="json"),
            runtime_identity_digest=canonical_digest(identity),
            execution_contract_digest=canonical_digest(
                parse_execution_contract(task.execution_contract)
            ),
        )
    except (ScenePackageError, OSError) as exc:
        finding = (
            exc.finding
            if isinstance(exc, ScenePackageError)
            else {
                "code": "scene_io_error",
                "path": "runtime",
                "message": "Check permissions/private diagnostics; retain failures.",
            }
        )
        report["issues"] = [finding]
    if writable:
        # Only our projected report is written to the public-facing destination.
        try:
            fd = os.open(
                public, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
            )
            with os.fdopen(fd, "w") as stream:
                json.dump(report, stream, sort_keys=True, indent=2, allow_nan=False)
                stream.write("\n")
        except OSError:
            report.update(
                ok=False,
                issues=[
                    {
                        "code": "report_path",
                        "path": "output",
                        "message": "Choose a new writable public report path.",
                    }
                ],
            )
    return report
