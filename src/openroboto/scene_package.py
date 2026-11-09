"""Materialize and measure schema-3 submissions, without importing a simulator.

All wire schemas, resource guards, dependency hashes and identities come from
the installed protocol. These checks establish structure only. Native model
compilation, control, judging and reference replay belong to the worker runtime.
"""

from __future__ import annotations

import json
import os
import pathlib
import stat
from dataclasses import dataclass
from typing import Any
from xml.etree import ElementTree

from openroboto_protocol.scene_contract import (
    SubmissionIdentity,
    parse_execution_contract,
    profile_catalog,
)
from openroboto_protocol.scene_schema import (
    PrivateReferenceBinding,
    SceneTask,
    SubmissionManifest,
    dependency_issues,
    submission_issues,
)
from openroboto_protocol.tabletop_assets import canonical_digest
from pydantic import ValidationError

from openroboto.task_package import package_digest


class ScenePackageError(ValueError):
    """An actionable finding that never contains private payload contents."""

    def __init__(self, code: str, path: str, message: str):
        self.finding = {"code": code, "path": path, "message": message}
        super().__init__(f"[{code}] {path}: {message}")


def _read_regular(path: pathlib.Path, *, limit: int) -> bytes:
    """Bound actual file bytes and reject special files before any read.

    Recheck the opened descriptor: a replacement after lstat must not turn a
    validated regular file into a blocking FIFO, symlink or oversized input.
    """

    def check(info: os.stat_result) -> None:
        if not stat.S_ISREG(info.st_mode):
            raise ScenePackageError(
                "path_unsafe",
                "package",
                "Use regular files, not links or special files.",
            )
        if info.st_size > limit:
            raise ScenePackageError(
                "resource_limit", "package", "Actual bytes exceed the resource profile."
            )

    check(path.lstat())
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        check(os.fstat(descriptor))
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            data = stream.read(limit + 1)
    finally:
        os.close(descriptor)
    if len(data) > limit:
        raise ScenePackageError(
            "resource_limit", "package", "Actual bytes exceed the resource profile."
        )
    return data


def _json(path: pathlib.Path) -> Any:
    try:
        limit = max(
            p["max_package_bytes"] for p in profile_catalog()["resources"].values()
        )
        return json.loads(_read_regular(path, limit=limit))
    except ScenePackageError:
        raise
    except (OSError, ValueError) as exc:
        raise ScenePackageError(
            "json_unreadable", path.name, "Provide a readable UTF-8 JSON document."
        ) from exc


def _tree(root: pathlib.Path, *, limit: int) -> dict[str, bytes]:
    """Read actual bytes with a bounded total and no symlink/special-file escape."""
    if root.is_symlink() or not root.is_dir():
        raise ScenePackageError("root_invalid", "package", "Use a regular directory.")
    files: dict[str, bytes] = {}
    total = 0
    for path in sorted(root.rglob("*")):
        mode = path.lstat().st_mode
        relative = path.relative_to(root).as_posix()
        if path.is_symlink() or not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
            raise ScenePackageError(
                "path_unsafe",
                "package",
                "Replace symlinks and special files with bytes.",
            )
        if any(p in {"", ".", ".."} for p in relative.split("/")) or any(
            c in relative for c in ("\\", "\0")
        ):
            raise ScenePackageError(
                "path_unsafe", "package", "Use package-relative paths."
            )
        if stat.S_ISDIR(mode):
            continue
        # Model cards can bypass HF gating. Keep public text in public_summary.
        if path.name.casefold() == "readme.md":
            raise ScenePackageError(
                "public_card_forbidden", "package", "Remove README.md before uploading."
            )
        data = _read_regular(path, limit=limit - total)
        total += len(data)
        files[relative] = data
    return files


def execution_input(root: pathlib.Path) -> tuple[SceneTask, dict[str, bytes]]:
    """Validate schema, declared byte dependencies and XML resource edges."""
    if root.is_symlink() or not root.is_dir() or (root / "task.json").is_symlink():
        raise ScenePackageError(
            "root_invalid", "execution", "Use a regular input tree."
        )
    try:
        task = SceneTask.model_validate(_json(root / "task.json"))
    except ValidationError as exc:
        finding = exc.errors(include_input=False, include_context=False)[0]
        raise ScenePackageError(
            "scene_invalid",
            ".".join(map(str, finding["loc"])),
            "Match the installed protocol SceneTask schema; inspect this field "
            "locally.",
        ) from exc
    limit = profile_catalog()["resources"][task.task_definition.resource_profile][
        "max_package_bytes"
    ]
    files = _tree(root, limit=limit)
    issues = dependency_issues(task, files)
    if issues:
        i = issues[0]
        raise ScenePackageError(i.code, i.path, i.message)
    declared = {item.path: set(item.requires) for item in task.scene.dependencies}
    for path, edges in declared.items():
        if path != task.scene.entrypoint and not path.lower().endswith(".xml"):
            continue
        data = files[path]
        if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
            raise ScenePackageError(
                "xml_entity_forbidden", path, "Remove XML entities."
            )
        try:
            model = ElementTree.fromstring(data)
        except ElementTree.ParseError as exc:
            raise ScenePackageError(
                "xml_invalid", path, "Repair the XML document."
            ) from exc
        for element in model.iter():
            target = element.get("file")
            if target is not None and (target not in declared or target not in edges):
                raise ScenePackageError(
                    "dependency_undeclared",
                    path,
                    "Declare every XML file reference and its direct dependency edge.",
                )
            # The contract resolves from execution/, never compiler search paths.
            if element.tag == "compiler" and any(
                element.get(key) for key in ("assetdir", "meshdir", "texturedir")
            ):
                raise ScenePackageError(
                    "dependency_search_path", path, "Use root-relative XML file paths."
                )
    return task, files


def _reference(files: dict[str, bytes], binding: PrivateReferenceBinding) -> None:
    for name in binding.reference_files:
        try:
            value = json.loads(files[name])
        except (KeyError, ValueError) as exc:
            raise ScenePackageError(
                "reference_unreadable",
                "private",
                "Materialize every bound JSON reference.",
            ) from exc
        if (
            not isinstance(value, dict)
            or type(value.get("schema_version")) is not int
            or value["schema_version"] != binding.reference_schema_version
        ):
            raise ScenePackageError(
                "reference_schema",
                "private",
                "Use the declared reference schema version.",
            )


@dataclass(frozen=True)
class SceneSubmission:
    root: pathlib.Path
    task: SceneTask
    manifest: SubmissionManifest

    def report(self) -> dict[str, Any]:
        """Only identities and independently named gates, never reference text."""
        return {
            "ok": True,
            "scope": "structure_only",
            "submission": self.manifest.model_dump(mode="json"),
            "execution_profile": self.task.execution_profile.model_dump(),
            "local_physics": "not_run",
            "server_admission": "not_run",
            "reward_status": "not_evaluated",
        }


def load_submission(root: str | pathlib.Path) -> SceneSubmission:
    directory = pathlib.Path(root).expanduser().absolute()
    if directory.is_symlink():
        raise ScenePackageError("root_invalid", "package", "Do not use a symlink root.")
    task, _ = execution_input(directory / "execution")
    limit = profile_catalog()["resources"][task.task_definition.resource_profile][
        "max_package_bytes"
    ]
    files = _tree(directory, limit=limit)
    if any(
        name not in {"submission.json", ".gitattributes"}
        and not name.startswith(("execution/", "private/"))
        for name in files
    ):
        raise ScenePackageError(
            "detached_layout",
            "package",
            "Use execution/, private/ and submission.json.",
        )
    try:
        manifest = SubmissionManifest.model_validate_json(files["submission.json"])
        binding = PrivateReferenceBinding.model_validate_json(
            files["private/reference-binding.json"]
        )
    except (KeyError, ValidationError) as exc:
        raise ScenePackageError(
            "binding_invalid",
            "submission",
            "Rebuild both roots and their detached binding.",
        ) from exc
    private = {
        p.removeprefix("private/"): b
        for p, b in files.items()
        if p.startswith("private/")
    }
    _reference(private, binding)
    issues = submission_issues(
        manifest,
        binding,
        execution_package_digest=package_digest(directory / "execution"),
        private_package_digest=package_digest(directory / "private"),
        execution_contract=task.execution_contract,
    )
    if issues:
        i = issues[0]
        raise ScenePackageError(i.code, i.path, "Rebuild after changing either root.")
    return SceneSubmission(directory, task, manifest)


def _write(path: pathlib.Path, value: Any) -> None:
    path.write_text(json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n")


def build_submission(
    execution: pathlib.Path,
    output: pathlib.Path,
    *,
    reference: pathlib.Path | None = None,
    private: pathlib.Path | None = None,
) -> SceneSubmission:
    """Copy materialized inputs; bind references after measuring execution bytes."""
    task, execution_files = execution_input(execution)
    if output.exists() or output.is_symlink():
        raise ScenePackageError(
            "output_exists", "output", "Choose a new output directory."
        )
    limit = profile_catalog()["resources"][task.task_definition.resource_profile][
        "max_package_bytes"
    ]
    if private is not None:
        private_files = _tree(private, limit=limit)
        try:
            binding = PrivateReferenceBinding.model_validate_json(
                private_files["reference-binding.json"]
            )
        except (KeyError, ValidationError) as exc:
            raise ScenePackageError(
                "binding_invalid", "private", "Supply a schema-1 reference binding."
            ) from exc
        names = binding.reference_files
    else:
        if reference is None or reference.is_symlink() or not reference.is_file():
            raise ScenePackageError(
                "reference_missing", "private", "Select a regular reference JSON."
            )
        data = _read_regular(reference, limit=limit)
        private_files = {"reference.json": data}
        names = ["reference.json"]
    output = output.resolve()
    for source in (execution, private, reference):
        if source is not None and output.is_relative_to(source.resolve()):
            raise ScenePackageError(
                "output_nested", "output", "Keep output outside input trees."
            )
    # Failure artifacts are intentionally retained. Never replace an existing tree.
    output.mkdir(parents=True, mode=0o700)
    for name, content in {
        **{"execution/" + p: b for p, b in execution_files.items()},
        **{"private/" + p: b for p, b in private_files.items()},
    }.items():
        target = output / name
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        target.write_bytes(content)
        target.chmod(0o600)
    execution_digest = package_digest(output / "execution")
    binding_doc = {
        "schema_version": 1,
        "execution_package_digest": execution_digest,
        "execution_contract_digest": canonical_digest(
            parse_execution_contract(task.execution_contract)
        ),
        "reference_schema_version": 1,
        "reference_files": names,
    }
    _write(output / "private/reference-binding.json", binding_doc)
    identity = SubmissionIdentity(execution_digest, package_digest(output / "private"))
    _write(
        output / "submission.json",
        {
            "schema_version": 1,
            "execution_package_digest": identity.execution_package_digest,
            "private_package_digest": identity.private_package_digest,
            "submission_digest": identity.digest,
        },
    )
    return load_submission(output)
