"""Bridge canonical instruction generation; the renderer lives in the runtime.

Only explicitly requested NEW E materialization happens here. Program text,
symbol resolution and admission policy are never reimplemented by the CLI.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import pathlib
import re
import shutil
import tempfile
import time
from typing import Any

from openroboto_protocol.scene_schema import SceneTask
from openroboto_protocol.tabletop_assets import canonical_digest
from pydantic import ValidationError

from openroboto import scene_authoring as runtime
from openroboto.scene_package import (
    ScenePackageError,
    _read_regular,
    contents_digest,
    execution_input,
)

INSTRUCTION_MESSAGES = {
    "scene_instruction_rendered": "Full canonical instruction rendered.",
    "scene_instruction_length_unsupported": (
        "Full text exceeds the registered capacity; do not truncate it."
    ),
    "scene_instruction_vision_unsupported": (
        "This policy does not support canonical visual instructions."
    ),
    "scene_instruction_missing_binding": (
        "The renderer lacks a required public scene binding."
    ),
    "scene_instruction_semantics_unsupported": (
        "The renderer cannot fully express this program's semantics."
    ),
    "scene_instruction_profile_unsupported": (
        "Use the runtime's exact registered scene profile."
    ),
    "scene_instruction_policy_unsupported": (
        "Install the reviewed registered instruction policy."
    ),
    "scene_instruction_renderer_unsupported": (
        "Install the reviewed registered renderer."
    ),
}


def instruction_bytes(source: bytes, instruction: str) -> bytes:
    """Replace only the top-level JSON string token, preserving all other bytes.

    JSONDecoder locates value boundaries, including escaped quotes and nested
    values. Other numeric tokens are not serialized, rounded or reformatted.
    Ambiguous duplicate keys and non-UTF-8 input cannot be safely prepared.
    """

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    def constant(_: str) -> None:
        raise ValueError("nonfinite JSON")

    try:
        text = source.decode("utf-8")
        decoder = json.JSONDecoder(object_pairs_hook=pairs, parse_constant=constant)
        offset = 1 if text.startswith("\ufeff") else 0

        def skip(index: int) -> int:
            while index < len(text) and text[index] in " \t\r\n":
                index += 1
            return index

        offset = skip(offset)
        document, end = decoder.raw_decode(text, offset)
        if not isinstance(document, dict) or skip(end) != len(text):
            raise ValueError("object required")
        offset += 1
        seen: set[str] = set()
        span = None
        while True:
            offset = skip(offset)
            if text[offset] == "}":
                break
            key, offset = decoder.raw_decode(text, offset)
            if not isinstance(key, str) or key in seen:
                raise ValueError("unique string key required")
            seen.add(key)
            offset = skip(offset)
            if text[offset] != ":":
                raise ValueError("colon required")
            start = skip(offset + 1)
            value, offset = decoder.raw_decode(text, start)
            if key == "instruction":
                if not isinstance(value, str):
                    raise ValueError("instruction string required")
                span = (start, offset)
            offset = skip(offset)
            if text[offset] == "}":
                break
            if text[offset] != ",":
                raise ValueError("comma required")
            offset += 1
        if skip(offset + 1) != len(text) or span is None:
            raise ValueError("complete instruction object required")
        replacement = json.dumps(instruction, ensure_ascii=False, allow_nan=False)
        return (text[: span[0]] + replacement + text[span[1] :]).encode("utf-8")
    except (ValueError, IndexError, RecursionError) as exc:
        raise ScenePackageError(
            "instruction_input_invalid",
            "execution/task.json",
            "Use unambiguous UTF-8 JSON with one top-level instruction string.",
        ) from exc


def prepare_execution(
    source: pathlib.Path,
    destination: pathlib.Path,
    files: dict[str, bytes],
    instruction: str,
) -> None:
    """Create only NEW E; never copy P, rebind proof or rewrite the source tree."""
    if destination.exists() or destination.is_symlink():
        raise ScenePackageError(
            "output_exists", "prepared_execution", "Choose a NEW execution directory."
        )
    a, b = source.resolve(), destination.resolve()
    if a == b or a.is_relative_to(b) or b.is_relative_to(a):
        raise ScenePackageError(
            "output_layout_invalid",
            "prepared_execution",
            "Keep new E outside source E.",
        )
    prepared = {
        **files,
        "task.json": instruction_bytes(files["task.json"], instruction),
    }
    # Keep the installed protocol's Text limit and every other schema constraint.
    from openroboto_protocol.scene_schema import SceneTask

    try:
        SceneTask.model_validate(json.loads(prepared["task.json"]))
    except ValidationError as exc:
        raise ScenePackageError(
            "instruction_unsupported",
            "instruction",
            "Full canonical text does not fit the unchanged schema; do not shorten it.",
        ) from exc
    destination.mkdir(parents=True, mode=0o700)
    for name, content in prepared.items():
        path = destination / name
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
    execution_input(destination)


def _public_rendering(
    body: dict[str, Any],
    task: SceneTask,
    digest: str,
    identity: dict[str, Any],
    task_bytes: bytes,
) -> dict[str, Any]:
    """Consume the fixed runtime wrapper, never a second renderer/wire format."""
    from openroboto_protocol.scene_contract import profile_ref

    try:
        finding = body["instruction"]
        if not isinstance(finding, dict):
            raise ValueError("instruction object required")
        policy = identity["description"]["instruction_admission_policy"]
        configuration = policy["configuration"]
        accepted, text = finding["accepted"], finding["text"]
        if not (
            isinstance(policy, dict)
            and isinstance(configuration, dict)
            and canonical_digest(policy)
            == canonical_digest(
                identity["description"]["registry"]["runtime"][
                    "instruction_admission_policy"
                ]
            )
            and policy["digest"] == canonical_digest(configuration)
            and configuration["execution_profile"] == profile_ref()
            and configuration["policy_version"] == policy["version"]
            and configuration["renderer"] == policy["renderer_version"]
            and configuration["max_instruction_characters"]
            == identity["metadata"]["profile"]["instruction"]["max_characters"]
            and type(body["schema_version"]) is int
            and body["schema_version"] == 1
            and body["command"] == "scene-render-instruction"
            and type(accepted) is bool
            and body["ok"] is accepted
            and body["capability"]
            == ("instruction_render" if accepted else "unsupported")
            and body["runtime_version"] == identity["metadata"]["runtime_version"]
            and body["execution_profile"] == profile_ref()
            and body["installed_environment"] == task.environment.model_dump()
            and body["input_environment"] == task.environment.model_dump()
            and body["environment_matches_installation"] is True
            and body["input_task_sha256"] == hashlib.sha256(task_bytes).hexdigest()
            and body["package_validated"] is False
            and body["private_reference_created"] is False
            and body["server_admission"] == "not_run"
            and type(body["business_model_calls"]) is int
            and body["business_model_calls"] == 0
            and finding["schema_valid"] is True
            and isinstance(finding["code"], str)
            and (not accepted or finding["code"] == "scene_instruction_rendered")
            and type(finding["max_characters"]) is int
            and finding["max_characters"] == configuration["max_instruction_characters"]
            and finding["program_digest"]
            == canonical_digest(task.task_definition.model_dump(mode="json"))
            and re.fullmatch("[0-9a-f]{64}", finding["render_input_digest"])
            and finding["policy_version"] == policy["version"]
            and finding["policy_digest"] == policy["digest"]
            and finding["renderer_version"] == policy["renderer_version"]
            and finding["renderer_implementation_digest"]
            == policy["renderer_implementation_digest"]
            and all(
                re.fullmatch("[0-9a-f]{64}", policy[key])
                for key in (
                    "renderer_implementation_digest",
                    "installation_manifest_digest",
                )
            )
            # This is generation, not source consistency or actual model delivery.
            and finding["actual_instruction_digest"] is None
            and finding["delivered_instruction_digest"] is None
            and finding["delivery_checked"] is False
        ):
            raise ValueError("instruction identity")
        if text is not None:
            if not (
                isinstance(text, str)
                and type(finding["character_count"]) is int
                and finding["character_count"] == len(text)
                and finding["expected_instruction_digest"]
                == hashlib.sha256(text.encode("utf-8")).hexdigest()
            ):
                raise ValueError("full text identity")
        elif (
            accepted
            or finding["character_count"] is not None
            or finding["expected_instruction_digest"] is not None
        ):
            raise ValueError("missing full text")
        if accepted:
            document = task.model_dump(mode="json")
            document["instruction"] = text
            SceneTask.model_validate(document)
        keys = (
            "accepted",
            "schema_valid",
            "text",
            "character_count",
            "max_characters",
            "program_digest",
            "render_input_digest",
            "expected_instruction_digest",
            "actual_instruction_digest",
            "delivered_instruction_digest",
            "delivery_checked",
            "policy_version",
            "policy_digest",
            "renderer_version",
            "renderer_implementation_digest",
        )
        result = {key: finding[key] for key in keys}
        result["code"] = (
            finding["code"]
            if finding["code"] in INSTRUCTION_MESSAGES or accepted
            else "instruction_unsupported"
        )
        for key in (
            "schema_version",
            "command",
            "runtime_version",
            "execution_profile",
            "installed_environment",
            "input_environment",
            "environment_matches_installation",
            "input_task_sha256",
            "package_validated",
            "private_reference_created",
            "server_admission",
            "business_model_calls",
        ):
            result[key] = body[key]
        # Retain the CLI's earlier text alias; only the native nested wrapper is read.
        result.update(
            instruction=text,
            environment=body["input_environment"],
            execution_package_digest=digest,
            installation_manifest_digest=policy["installation_manifest_digest"],
        )
        return result
    except ValidationError as exc:
        raise ScenePackageError(
            "instruction_unsupported",
            "instruction",
            "Full canonical instruction exceeds the unchanged schema. "
            "Do not truncate it.",
        ) from exc
    except (KeyError, TypeError, ValueError) as exc:
        raise ScenePackageError(
            "instruction_result_invalid",
            "runtime",
            "Expected the registered runtime wrapper, complete public text "
            "and exact input/policy/source identities.",
        ) from exc


def run_instruction(
    args: argparse.Namespace, *, runtime_workdir: pathlib.Path | None = None
) -> dict[str, Any]:
    """Ask the same runtime for public text; create no P, proof or physics result."""
    report: dict[str, Any] = {
        "ok": False,
        "scope": "canonical_instruction",
        "capability": "unsupported",
        "local_physics": "not_run",
        "server_admission": "not_run",
        "reward_status": "not_evaluated",
        "issues": [],
    }
    output = args.output.expanduser().absolute()
    source = args.execution.expanduser().absolute()
    prepared = (
        args.prepared_execution.expanduser().absolute()
        if args.prepared_execution
        else None
    )
    writable = False
    try:
        runtime._new(output)
        paths = [source, output] + ([prepared] if prepared else [])
        runtime._layout(paths)
        if source.name == "execution" and any(
            p.resolve().is_relative_to(source.parent.resolve()) for p in paths[1:]
        ):
            raise ScenePackageError(
                "output_layout_invalid",
                "output",
                "Keep public output and NEW E outside the retained submission tree.",
            )
        if not output.parent.is_dir():
            raise ScenePackageError(
                "report_path", "output", "Create the report parent directory first."
            )
        if prepared:
            runtime._new(prepared)
        writable = True
        task, files = execution_input(source)
        digest = contents_digest(files)
        report["source_execution_package_digest"] = digest
        report["source_structure_checked"] = True
        if (
            not math.isfinite(args.timeout_seconds)
            or not 0 < args.timeout_seconds <= 60
        ):
            raise ScenePackageError(
                "runtime_budget_invalid",
                "runtime",
                "Use a pure-command deadline in (0, 60] seconds.",
            )
        python = shutil.which(str(pathlib.Path(args.runtime_python).expanduser()))
        if python is None:
            runtime._fail(
                "runtime_unavailable", "Choose an installed --runtime-python."
            )
        deadline = time.monotonic() + args.timeout_seconds
        identity = runtime._probe(python, task, deadline)
        if "scene-render-instruction" not in identity["description"]["commands"]:
            runtime._fail(
                "scene_instruction_unavailable",
                "This exact runtime lacks scene-render-instruction. "
                "Install the reviewed renderer runtime; do not guess text.",
            )
        with tempfile.TemporaryDirectory(
            prefix="openroboto-scene-instruction-", dir=runtime_workdir
        ) as temporary:
            public = pathlib.Path(temporary) / "instruction.json"
            if contents_digest(execution_input(source)[1]) != digest:
                runtime._fail("execution_drift", "Source E changed before rendering.")
            try:
                rc, _ = runtime._invoke(
                    python,
                    [
                        "-m",
                        "task_source.authoring",
                        "scene-render-instruction",
                        "--execution",
                        str(source),
                        "--output",
                        str(public),
                    ],
                    timeout=runtime._remaining(deadline),
                )
            finally:
                if contents_digest(execution_input(source)[1]) != digest:
                    runtime._fail(
                        "execution_drift",
                        "Runtime changed source E; retain the failure.",
                    )
            body = runtime._decode(
                _read_regular(public, limit=runtime.MAX_REPORT_BYTES)
            )
            if canonical_digest(
                runtime._probe(python, task, deadline)
            ) != canonical_digest(identity):
                runtime._fail(
                    "runtime_identity_drift",
                    "The exact runtime changed during rendering.",
                )
            if contents_digest(execution_input(source)[1]) != digest:
                runtime._fail(
                    "execution_drift", "Source E changed during the final probe."
                )
            rendered = _public_rendering(
                body, task, digest, identity, files["task.json"]
            )
            report["rendering"] = rendered
            if rendered["accepted"] is not True:
                code = rendered["code"]
                runtime._fail(
                    code,
                    INSTRUCTION_MESSAGES.get(
                        code,
                        "The renderer does not support this complete program; "
                        "no model fallback.",
                    ),
                )
            if rc != 0:
                runtime._fail(
                    "runtime_exit_mismatch",
                    "Render process exit disagrees with its public receipt.",
                )
            if prepared:
                prepare_execution(source, prepared, files, rendered["instruction"])
                report.update(
                    prepared_execution=str(prepared),
                    prepared_execution_package_digest=contents_digest(
                        execution_input(prepared)[1]
                    ),
                    reference_re_record_required=True,
                )
            if contents_digest(execution_input(source)[1]) != digest:
                runtime._fail("execution_drift", "Source E changed during preparation.")
            report.update(
                ok=True,
                capability="development",
                rendering=rendered,
                runtime_identity_digest=canonical_digest(identity),
            )
    except (ScenePackageError, OSError) as exc:
        report["issues"] = [
            exc.finding
            if isinstance(exc, ScenePackageError)
            else {
                "code": "scene_io_error",
                "path": "instruction",
                "message": "Check file permissions; keep failed output for diagnosis.",
            }
        ]
    if writable:
        try:
            fd = os.open(
                output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
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
