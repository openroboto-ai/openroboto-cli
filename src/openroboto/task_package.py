"""Task packages: the directory format a task season evaluates, and its digest.

A task season (``competitions.track = "task"``) does not take a model. The
on-chain commitment names a HuggingFace repository at one commit, exactly as a
model submission does, but that repository holds a **task package**: a
``task.json`` plus the instance payloads, manifests and (for AXIS) the scene and
reference solution.

## The digest is a cross-repository contract

🔴 **The shared protocol implementation is the only implementation.**
OPE-211 added ``openroboto_protocol.package_digest`` (candidate 0.13.0), with
the same rule as the validator's ``task_source/package.py`` at ``e5fee86a``.
This module imports it and delegates; ``digest_source()`` reports
``openroboto-protocol``. **There is no local fallback.** The published 0.12.0
pin predates the module, so task digest commands refuse with the release/
candidate instruction until 0.13.0 is published and the pin moves. The golden
value in ``tests/test_task_package.py`` and the candidate-wheel comparison in
``scripts/verify_task_cli_install.py`` both check the shared function.

Three layers compute the same number and must agree byte for byte:

* the admission worker reports ``package_digest``;
* the backend stores it as ``tasks.package_digest``;
* the evaluation layer pins it as ``EvaluationSpec.task.version_digest``.

The one implementation is the validator's ``task_source/package.py``; the
backend stores what the validator reported and never recomputes it. This module
is the **miner-side** copy, because a miner has to be able to compute the same
number before paying an entry fee. The rule is therefore not "similar", it is
the same:

1. walk **every regular file** in the package directory, recursively, with no
   whitelist and no exclusions (``reference.json``, ``scene.xml``, a stray
   ``.DS_Store`` all count);
2. sort the paths **as paths**, not as strings;
3. per file, ``sha256(read_bytes())``;
4. digest the UTF-8 bytes of ``"\\n".join(f"{path}\\0{file_sha256}")``.

The sorting detail is not pedantry: ``Path`` sorts by parts, so ``a-b/x`` and
``a/b`` order differently under a string sort. A second implementation that
sorts strings produces a different number on exactly the packages that have
mixed nesting, and the miner pays for the difference.

## What is validated here, and what is not

``validate_package`` answers "is this a well-formed package of the declared
source" -- required files, declared instance counts, payload references and the
canonical hashes the manifests claim. It deliberately does **not** answer
"is this a good task": solvable / not-a-freebie / determinism are the
validator's runtime admission (``task_source/admission.py``), and claiming them
from a static check would be the same kind of lie as a mocked commitment.

Runtime is not verified here either. An AXIS package needs MuJoCo and a
RoboDojo package needs Isaac Sim; this module touches neither.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import shutil
import stat
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final

#: The shared implementation, when the installed protocol release has it.
#: OPE-211 adds it as ``openroboto_protocol.package_digest`` in 0.13.0. The
#: published 0.12.0 pin predates it, so the task commands refuse clearly until
#: the pin moves. **No local fallback exists** -- a second implementation of a
#: rule three consumers must agree on is the drift this package prevents.
_shared_package_files: (
    Callable[[str | pathlib.Path], tuple[tuple[str, str], ...]] | None
) = None
_shared_digest_of_files: Callable[[tuple[tuple[str, str], ...]], str] | None = None
_shared_package_digest: Callable[[str | pathlib.Path], str] | None = None
try:  # pragma: no cover - which branch runs depends on the installed wheel
    from openroboto_protocol.package_digest import (
        digest_of_files as _protocol_digest_of_files,
    )
    from openroboto_protocol.package_digest import (
        package_digest as _protocol_package_digest,
    )
    from openroboto_protocol.package_digest import (
        package_files as _protocol_package_files,
    )

    _shared_package_files = _protocol_package_files
    _shared_digest_of_files = _protocol_digest_of_files
    _shared_package_digest = _protocol_package_digest
except ImportError:
    pass

TASK_FILE: Final = "task.json"
SCHEMA_VERSION: Final = 1

#: The task sources this client knows how to inspect. **Not an enum on
#: purpose**: sources are added by a config row, not a migration (the backend's
#: ``tasks.source`` is ``text`` for the same reason). An unknown source is
#: refused with the list rather than guessed.
TASK_SOURCES: Final = ("axis", "robodojo")

#: The eight checker types the AXIS vocabulary admits. Copied from the
#: validator's ``task_source/axis.py`` because it is a wire vocabulary: a wrong
#: guess here would validate a package the validator then rejects.
AXIS_CHECKER_TYPES: Final = (
    "CompositeChecker",
    "RelativeCylinderChecker",
    "RelativePositionBoundsChecker",
    "DirectedRotationChecker",
    "GripperOpenChecker",
    "DrawerBBoxChecker",
    "FrameBBoxChecker",
    "SamplePositionDeltaChecker",
)

ROBODOJO_PINNED_REF: Final = "9226f48ea694b3f53db12d4922e8b1199f8d0891"
"""The RoboDojo commit the validator pins. A package that names another commit
is not wrong by itself -- it is a package for a different runtime -- so this is
reported as a warning, not an error, and only when the declared value differs."""


class TaskPackageError(Exception):
    """The directory is not a task package at all (no readable ``task.json``)."""


class PackageFetchError(Exception):
    """``repo@commit`` could not be downloaded, so nothing could be judged."""


@dataclass(frozen=True)
class Issue:
    """One finding. ``severity == "error"`` means the validator would refuse."""

    code: str
    message: str
    severity: str = "error"

    @property
    def is_error(self) -> bool:
        return self.severity == "error"


@dataclass(frozen=True)
class TaskPackage:
    directory: pathlib.Path
    task: Mapping[str, Any]
    source: str
    task_id: str
    instruction: str

    def path(self, relative: str) -> pathlib.Path:
        """Resolve a package-relative path, refusing anything that escapes."""
        candidate = (self.directory / relative).resolve()
        try:
            candidate.relative_to(self.directory.resolve())
        except ValueError as exc:
            raise TaskPackageError(
                f"package path escapes task directory: {relative!r}"
            ) from exc
        return candidate


# ── hashing ──────────────────────────────────────────────────────────────────


def canonical_json_sha256(value: Any) -> str:
    """The canonical-JSON SHA-256 the AXIS manifests declare.

    Same rule as the AXIS evaluation program: ``ensure_ascii=False``, compact
    separators, sorted keys, UTF-8.
    """
    encoded = json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


_NO_SHARED = (
    "The installed openroboto-protocol does not provide "
    "`openroboto_protocol.package_digest`, which the task track needs. The "
    "production pin is still the published 0.12.0; the shared implementation "
    "ships in the OPE-211 candidate 0.13.0, which is **not published on PyPI**.\n"
    "   → run the CLI against the candidate wheel in a development environment "
    "(`scripts/verify_task_cli_install.py`), or wait for the 0.13.0 release.\n"
    "   Nothing was paid, uploaded or sent on chain."
)


def digest_available() -> bool:
    """Whether the installed protocol provides the shared digest."""
    return (
        _shared_package_files is not None
        and _shared_digest_of_files is not None
        and _shared_package_digest is not None
    )


def digest_source() -> str:
    """Which implementation the next digest call will use."""
    return (
        "openroboto-protocol"
        if digest_available()
        else "unavailable(protocol release 0.13.0 needed)"
    )


def package_files(directory: str | pathlib.Path) -> tuple[tuple[str, str], ...]:
    """``(relative POSIX path, sha256)`` for every regular file, path-sorted."""
    if _shared_package_files is None:
        raise TaskPackageError(_NO_SHARED)
    return _shared_package_files(directory)


def digest_of_files(files: tuple[tuple[str, str], ...]) -> str:
    if _shared_digest_of_files is None:
        raise TaskPackageError(_NO_SHARED)
    return _shared_digest_of_files(files)


def package_digest(directory: str | pathlib.Path) -> str:
    """The task package's content identity, from the shared protocol module.

    There is **no local fallback**: a second implementation of a rule the
    validator, backend and CLI must agree on is the drift the protocol package
    exists to prevent. On the published 0.12.0 pin this raises with the
    release/candidate instruction until 0.13.0 is published and the pin moves.
    """
    if _shared_package_digest is None:
        raise TaskPackageError(_NO_SHARED)
    return _shared_package_digest(directory)


# ── loading ──────────────────────────────────────────────────────────────────


def read_json(path: pathlib.Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise TaskPackageError(f"missing file: {path}") from exc
    except ValueError as exc:
        raise TaskPackageError(f"invalid JSON: {path}: {exc}") from exc


def load_package(directory: str | pathlib.Path) -> TaskPackage:
    """Read and check the fields every source shares.

    Raises ``TaskPackageError`` when the directory is not a package or
    ``task.json`` is not usable. Source-specific structure is
    ``validate_package``'s job, so one bad manifest does not hide the rest.
    """
    root = pathlib.Path(directory).expanduser().resolve()
    path = root / TASK_FILE
    if not path.is_file():
        raise TaskPackageError(f"{root} is not a task package: missing {TASK_FILE}")
    task = read_json(path)
    if not isinstance(task, Mapping):
        raise TaskPackageError(f"{path} must contain a JSON object")
    if task.get("schema_version") != SCHEMA_VERSION:
        raise TaskPackageError(
            f"{path}: unsupported schema_version {task.get('schema_version')!r} "
            f"(this client knows {SCHEMA_VERSION})"
        )
    source = task.get("source")
    if source not in TASK_SOURCES:
        raise TaskPackageError(
            f"{path}: source must be one of {list(TASK_SOURCES)}, got {source!r}"
        )
    task_id = task.get("task_id")
    if not isinstance(task_id, (str, int)) or not str(task_id).strip():
        raise TaskPackageError(f"{path}: task_id must be a non-empty string or integer")
    instruction = task.get("instruction")
    if not isinstance(instruction, str) or not instruction.strip():
        raise TaskPackageError(f"{path}: instruction must be a non-empty string")
    return TaskPackage(root, task, str(source), str(task_id), instruction.strip())


def fetch_package(
    repo_id: str,
    commit: str,
    *,
    token: str = "",
    cache_dir: str | pathlib.Path | None = None,
) -> pathlib.Path:
    """Download ``repo@commit`` and return materialized repository files.

    ``commit`` must be the full 40-character revision; a branch or tag would let
    the contents (and therefore the digest) move after the commitment is
    written. ``snapshot_download`` is used without ``local_dir`` so the returned
    snapshot contains **only repository files** -- ``local_dir`` writes
    ``.cache/huggingface`` metadata inside it, and the digest counts every file.
    HF cache blob links are copied into ordinary files before package path
    validation; local package escape checks remain unchanged.
    """
    if len(commit) != 40 or any(char not in "0123456789abcdef" for char in commit):
        raise PackageFetchError(
            f"{commit!r} is not a full 40-character lowercase commit; "
            "the package identity is `repo@commit`, not a branch or tag"
        )
    try:
        from huggingface_hub import snapshot_download
        from huggingface_hub.utils import disable_progress_bars
    except ImportError as exc:  # pragma: no cover -- huggingface_hub is a dependency
        raise PackageFetchError(
            "huggingface_hub is not installed, so a repository package cannot "
            "be fetched"
        ) from exc
    # `--json` must be one object on stdout; the download bars are noise for a
    # command whose output is meant to be pasted into an evidence record.
    disable_progress_bars()
    try:
        snapshot = pathlib.Path(
            snapshot_download(
                repo_id=repo_id,
                revision=commit,
                repo_type="model",
                token=token or None,
                cache_dir=str(cache_dir) if cache_dir is not None else None,
            )
        )
    except Exception as exc:  # huggingface_hub has no single error base class
        raise PackageFetchError(
            f"could not download {repo_id}@{commit[:8]}: {exc}"
        ) from exc
    return _materialize_snapshot(snapshot, repo_id, commit, cache_dir=cache_dir)


def _materialize_snapshot(
    snapshot: pathlib.Path,
    repo_id: str,
    commit: str,
    *,
    cache_dir: str | pathlib.Path | None,
    materialized_dir: pathlib.Path | None = None,
) -> pathlib.Path:
    """Copy repository content, allowing only HF's own regular blob links."""
    destination: pathlib.Path | None = None
    try:
        if snapshot.is_symlink():
            raise PackageFetchError("HF snapshot root must not be a symlink")
        root = snapshot.resolve(strict=True)
        if not root.is_dir():
            raise PackageFetchError("HF snapshot root is not a directory")
        # Only the standard layout grants access outside the snapshot tree.
        # Never treat an arbitrary parent directory as a cache authorization.
        blob_root = None
        from huggingface_hub.constants import HF_HUB_CACHE

        trusted_cache = pathlib.Path(cache_dir or HF_HUB_CACHE).resolve()
        if (
            root.name == commit
            and root.parent.name == "snapshots"
            and root.parent.parent.name == f"models--{repo_id.replace('/', '--')}"
            and root.parent.parent.parent == trusted_cache
        ):
            blobs = root.parent.parent / "blobs"
            if blobs.is_dir() and not blobs.is_symlink():
                blob_root = blobs.resolve(strict=True)
        for current, directories, files in os.walk(root, followlinks=False):
            for name in directories + files:
                path = pathlib.Path(current) / name
                mode = path.lstat().st_mode
                if stat.S_ISLNK(mode):
                    if blob_root is None or not _is_hf_blob_link(
                        path, blob_root, trusted_cache
                    ):
                        raise PackageFetchError(
                            f"unsafe HF snapshot symlink: {path.relative_to(root)}; "
                            "only regular files in this repository's blobs are allowed"
                        )
                elif not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                    raise PackageFetchError(
                        "HF snapshot contains a non-regular file: "
                        f"{path.relative_to(root)}"
                    )
        parent = materialized_dir
        if parent is None and cache_dir is not None:
            parent = pathlib.Path(cache_dir)
        if parent is not None:
            parent.mkdir(parents=True, exist_ok=True)
            if parent.resolve().is_relative_to(root):
                raise PackageFetchError(
                    "materialized files must be outside the HF snapshot"
                )
        destination = pathlib.Path(
            tempfile.mkdtemp(prefix="openroboto-task-files-", dir=parent)
        )
        # All repository files, including dotfiles, participate in the digest.
        # No SDK local_dir metadata or blobs directory is copied into this tree.
        shutil.copytree(root, destination, symlinks=False, dirs_exist_ok=True)
        return destination
    except (OSError, RuntimeError, shutil.Error) as exc:
        if destination is not None:
            shutil.rmtree(destination)
        raise PackageFetchError(
            f"could not materialize {repo_id}@{commit[:8]}: {exc}"
        ) from exc


def _is_hf_blob_link(
    path: pathlib.Path, blob_root: pathlib.Path, trusted_cache: pathlib.Path
) -> bool:
    """Accept snapshot -> repository blob [-> shared cache blob], nothing else."""
    # Normalize '..' without following the first link into an arbitrary path.
    blob = pathlib.Path(os.path.abspath(path.parent / os.readlink(path)))
    if blob.parent != blob_root:
        return False
    if blob.is_symlink():
        target = pathlib.Path(os.path.abspath(blob.parent / os.readlink(blob)))
        shared = trusted_cache / "blobs"
        if shared.is_symlink() or not target.is_relative_to(shared):
            return False
        # A second file link is HF's shared blob store. Directory aliases,
        # extra file links and cycles are not part of that layout.
        if target.is_symlink() or target.resolve(strict=True) != target:
            return False
        for parent in target.parents:
            if parent == shared:
                break
            if parent.is_symlink():
                return False
        blob = target
    return stat.S_ISREG(blob.lstat().st_mode)


def discover_packages(root: str | pathlib.Path) -> list[pathlib.Path]:
    """Directories holding a ``task.json``, one or two levels below ``root``."""
    base = pathlib.Path(root).expanduser()
    if (base / TASK_FILE).is_file():
        return [base]
    found: list[pathlib.Path] = []
    for depth in (1, 2):
        pattern = "/".join(["*"] * depth) + f"/{TASK_FILE}"
        found.extend(path.parent for path in base.glob(pattern) if path.is_file())
    return sorted(set(found))


# ── validation ───────────────────────────────────────────────────────────────


def checker_types_in(config: Any) -> set[str]:
    """Every ``type`` in a checker tree, including Composite children."""
    found: set[str] = set()

    def visit(node: Any) -> None:
        if isinstance(node, Mapping):
            if isinstance(node.get("type"), str):
                found.add(node["type"])
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)

    visit(config)
    return found


def select_package(
    root: str | pathlib.Path, *, source: str = "", relative: str = ""
) -> pathlib.Path:
    """The package directory a repository download names.

    The validator discovers packages at the repository root or one/two levels
    below and digests the **package** directory, not the repository root. Both
    sides must pick the same one for their digests to be comparable.

    ``source`` filters by the season's task source. Ambiguity is refused, not
    guessed: two packages of the same source in one repository cannot be told
    apart by anything this side knows, and the validator has the same problem.
    """
    base = pathlib.Path(root).expanduser().resolve()
    if not base.is_dir():
        raise TaskPackageError(f"{base} is not a directory")
    if relative:
        target = (base / relative).expanduser().resolve()
        try:
            target.relative_to(base)
        except ValueError as exc:
            raise TaskPackageError(f"package path {relative!r} escapes {base}") from exc
        if not (target / TASK_FILE).is_file():
            raise TaskPackageError(
                f"{target} is not a task package: missing {TASK_FILE}"
            )
        return target

    matches: list[tuple[pathlib.Path, str, str]] = []
    for directory in discover_packages(base):
        try:
            task = read_json(directory / TASK_FILE)
        except TaskPackageError:
            continue
        if not isinstance(task, Mapping):
            continue
        declared = str(task.get("source") or "")
        if source and declared != source:
            continue
        matches.append((directory, declared, str(task.get("task_id"))))

    if not matches:
        wanted = f" with source={source}" if source else ""
        raise TaskPackageError(
            f"no task package{wanted} found under {base}; the validator looks "
            f"for {TASK_FILE} at the root or one/two levels below"
        )
    if len(matches) > 1:
        listing = ", ".join(
            f"{directory.relative_to(base)} ({declared}/{task_id})"
            for directory, declared, task_id in matches
        )
        raise TaskPackageError(
            f"more than one task package under {base}: {listing}; name one "
            f"explicitly or filter by source"
        )
    return matches[0][0]


def validate_package(package: TaskPackage) -> list[Issue]:
    """Source-specific structure and manifest consistency.

    Errors are the ones the validator's static checks make terminal; warnings
    are facts a miner should see but that do not make the package invalid.
    """
    issues: list[Issue] = []
    if package.source == "axis":
        issues.extend(_validate_axis(package))
    elif package.source == "robodojo":
        issues.extend(_validate_robodojo(package))
    return issues


def _validate_axis(package: TaskPackage) -> list[Issue]:
    issues: list[Issue] = []
    task = package.task

    checker = task.get("checker")
    if not isinstance(checker, Mapping):
        issues.append(
            Issue("axis.checker_missing", "task.json has no `checker` object")
        )
        declared: set[str] = set()
    else:
        declared = checker_types_in(checker)
        unknown = sorted(declared - set(AXIS_CHECKER_TYPES))
        if not declared:
            issues.append(
                Issue("axis.checker_empty", "task.json `checker` declares no type")
            )
        if unknown:
            issues.append(
                Issue(
                    "axis.checker_unknown",
                    f"checker types not in the AXIS vocabulary: {unknown}",
                )
            )

    benchmark = _read_manifest(package, "benchmark.json", issues)
    randomization = _read_manifest(package, "randomization.json", issues)

    benchmark_task: Mapping[str, Any] = {}
    if benchmark is not None:
        matches = [
            item
            for item in benchmark.get("tasks") or []
            if isinstance(item, Mapping) and str(item.get("task_id")) == package.task_id
        ]
        if len(matches) != 1:
            issues.append(
                Issue(
                    "axis.benchmark_task",
                    f"benchmark.json must contain exactly one task with "
                    f"task_id={package.task_id}; found {len(matches)}",
                )
            )
        else:
            benchmark_task = matches[0]

    instances: list[Mapping[str, Any]] = []
    variants: list[Mapping[str, Any]] = []
    if randomization is not None:
        matches = [
            item
            for item in randomization.get("tasks") or []
            if isinstance(item, Mapping) and str(item.get("task_id")) == package.task_id
        ]
        if len(matches) != 1:
            issues.append(
                Issue(
                    "axis.randomization_task",
                    f"randomization.json must contain exactly one task with "
                    f"task_id={package.task_id}; found {len(matches)}",
                )
            )
        else:
            raw_variants = matches[0].get("variants")
            if not isinstance(raw_variants, list) or not raw_variants:
                issues.append(
                    Issue(
                        "axis.randomization_variants",
                        "randomization.json task has no `variants` list",
                    )
                )
            else:
                variants = [item for item in raw_variants if isinstance(item, Mapping)]

    if not 1 <= len(variants) <= 20:
        issues.append(
            Issue(
                "axis.instance_count",
                f"instance count must be between 1 and 20, got {len(variants)}",
            )
        )

    for variant in variants:
        issue = _validate_axis_variant(package, task, variant, declared)
        if issue is not None:
            issues.append(issue)
        payload = _payload_of(package, variant)
        if payload is not None:
            instances.append(payload)

    reference = task.get("reference_solution")
    if not reference:
        # The format allows an omitted reference, but the validator's runtime
        # admission then has no trajectory to replay and rejects as
        # `reject_unsolvable` (OPE-145's CPU run measured exactly that on a
        # package that passed every static check). Warning, not error: the
        # static format is valid, and the miner has to know the package is not
        # a complete submission.
        issues.append(
            Issue(
                "axis.reference_not_declared",
                "no reference solution; the validator's runtime admission will "
                "reject this as unsolvable unless a reference with per-instance "
                "`actions` is supplied",
                severity="warning",
            )
        )
    else:
        try:
            path = package.path(str(reference))
        except TaskPackageError as exc:
            issues.append(Issue("axis.reference_escapes", str(exc)))
        else:
            if not path.is_file():
                issues.append(
                    Issue(
                        "axis.reference_missing",
                        f"reference_solution names a missing file: {reference}",
                    )
                )
            else:
                issues.extend(_validate_axis_reference(package, path, variants))

    if instances and benchmark_task:
        first = instances[0]
        if benchmark_task.get("checker_sha256") != canonical_json_sha256(
            first.get("checker_config")
        ):
            issues.append(
                Issue(
                    "axis.manifest_checker_mismatch",
                    "benchmark.checker_sha256 does not match the first payload",
                )
            )
        if benchmark_task.get("initial_state_sha256") != canonical_json_sha256(
            first.get("initial_state") or {}
        ):
            issues.append(
                Issue(
                    "axis.manifest_state_mismatch",
                    "benchmark.initial_state_sha256 does not match the first payload",
                )
            )
        if str(benchmark_task.get("instruction")) != package.instruction:
            issues.append(
                Issue(
                    "axis.manifest_instruction_mismatch",
                    "benchmark.instruction does not match task.json",
                )
            )

    return issues


def _validate_axis_reference(
    package: TaskPackage,
    path: pathlib.Path,
    variants: list[Mapping[str, Any]],
) -> list[Issue]:
    """The reference must carry replayable actions for every instance.

    A declared `reference.json` with only `final_state` is the shape OPE-145's
    runtime admission rejected as `reject_unsolvable`: the checker layer can
    read it, but the simulator has no trajectory to replay. This is a static
    check of the shape; whether the actions actually succeed is runtime.
    """
    try:
        value = read_json(path)
    except TaskPackageError as exc:
        return [Issue("axis.reference_invalid", str(exc))]
    if not isinstance(value, Mapping):
        return [Issue("axis.reference_invalid", f"{path} must be a JSON object")]
    issues: list[Issue] = []
    if value.get("schema_version") != SCHEMA_VERSION:
        issues.append(
            Issue(
                "axis.reference_schema",
                f"{path}: unsupported schema_version {value.get('schema_version')!r}",
            )
        )
    if str(value.get("task_id")) != package.task_id:
        issues.append(
            Issue(
                "axis.reference_task_id",
                f"{path}: task_id {value.get('task_id')!r} != {package.task_id!r}",
            )
        )
    instances = value.get("instances")
    if not isinstance(instances, Mapping):
        issues.append(
            Issue("axis.reference_instances", f"{path}: no `instances` object")
        )
        return issues

    problems: list[str] = []
    for variant in variants:
        number = variant.get("instance")
        if not isinstance(number, int) or isinstance(number, bool):
            continue
        entry = instances.get(str(number))
        if not isinstance(entry, Mapping):
            problems.append(f"instance {number}: no entry")
            continue
        actions = entry.get("actions")
        if not isinstance(actions, list) or not actions:
            problems.append(f"instance {number}: `actions` missing or empty")
    if problems:
        issues.append(
            Issue(
                "axis.reference_actions_missing",
                "reference.json cannot be replayed: " + "; ".join(problems),
            )
        )
    return issues


def _validate_axis_variant(
    package: TaskPackage,
    task: Mapping[str, Any],
    variant: Mapping[str, Any],
    declared: set[str],
) -> Issue | None:
    instance = variant.get("instance")
    payload_path = variant.get("payload_path")
    if not isinstance(instance, int) or isinstance(instance, bool):
        return Issue(
            "axis.variant_instance",
            f"variant has no integer instance: {variant!r}",
        )
    if not isinstance(payload_path, str) or not payload_path:
        return Issue("axis.variant_payload", f"variant {instance} has no payload_path")
    try:
        path = package.path(payload_path)
    except TaskPackageError as exc:
        return Issue("axis.payload_escapes", f"instance {instance}: {exc}")
    if not path.is_file():
        return Issue(
            "axis.payload_missing",
            f"instance {instance}: payload_path {payload_path!r} does not exist",
        )
    try:
        payload = read_json(path)
    except TaskPackageError as exc:
        return Issue("axis.payload_invalid", f"instance {instance}: {exc}")
    if not isinstance(payload, Mapping):
        return Issue(
            "axis.payload_not_object", f"instance {instance}: payload is not an object"
        )

    problems: list[str] = []
    actual_payload = canonical_json_sha256(payload)
    if variant.get("payload_canonical_sha256") != actual_payload:
        problems.append(
            f"payload hash {actual_payload[:12]} != declared "
            f"{str(variant.get('payload_canonical_sha256'))[:12]}"
        )
    actual_checker = canonical_json_sha256(payload.get("checker_config"))
    if variant.get("checker_sha256") != actual_checker:
        problems.append(
            f"checker hash {actual_checker[:12]} != declared "
            f"{str(variant.get('checker_sha256'))[:12]}"
        )
    if str(payload.get("id")) != package.task_id:
        problems.append(f"payload id {payload.get('id')!r} != {package.task_id!r}")
    if payload.get("checker_config") != task.get("checker"):
        problems.append("payload checker_config differs from task.json")
    state_hash = canonical_json_sha256(payload.get("initial_state") or {})
    if variant.get("initial_state_sha256") is not None and state_hash != variant.get(
        "initial_state_sha256"
    ):
        problems.append("initial_state hash differs")
    scene = payload.get("scene")
    if not payload.get("mjcf_xml") and not (
        isinstance(scene, str) and scene and _inside_package(package, scene)
    ):
        problems.append("no mjcf_xml and no readable scene inside the package")
    if payload.get("checker_config") is not None:
        unknown = checker_types_in(payload.get("checker_config")) - declared
        if unknown:
            problems.append(
                f"payload declares checker types task.json does not: {unknown}"
            )
    if problems:
        return Issue(
            "axis.payload_mismatch",
            f"instance {instance}: " + "; ".join(problems),
        )
    return None


def _validate_robodojo(package: TaskPackage) -> list[Issue]:
    issues: list[Issue] = []
    task = package.task
    official = task.get("official_task")
    if not isinstance(official, str) or not official.strip():
        issues.append(
            Issue(
                "robodojo.official_task",
                "task.json must name the official RoboDojo task in `official_task`",
            )
        )
    elif official != package.task_id:
        issues.append(
            Issue(
                "robodojo.official_task_mismatch",
                f"official_task {official!r} != task_id {package.task_id!r}",
            )
        )

    dimension = task.get("dimension")
    if not isinstance(dimension, str) or not dimension.strip():
        issues.append(
            Issue(
                "robodojo.dimension",
                "task.json must declare a non-empty `dimension`",
            )
        )

    ref = task.get("robodojo_ref")
    if (
        not isinstance(ref, str)
        or len(ref) != 40
        or any(char not in "0123456789abcdef" for char in ref)
    ):
        issues.append(
            Issue(
                "robodojo.pinned_ref",
                "robodojo_ref must be a 40-character lowercase commit",
            )
        )
    elif ref != ROBODOJO_PINNED_REF:
        issues.append(
            Issue(
                "robodojo.pinned_ref_differs",
                f"robodojo_ref {ref} differs from the validator's pinned "
                f"{ROBODOJO_PINNED_REF}; the submitting runtime must run the "
                f"same commit",
                severity="warning",
            )
        )

    count = task.get("instance_count")
    if not isinstance(count, int) or isinstance(count, bool) or count <= 0:
        issues.append(
            Issue(
                "robodojo.instance_count",
                "instance_count must be a positive integer",
            )
        )

    protocol = task.get("protocol")
    if not isinstance(protocol, Mapping):
        issues.append(
            Issue("robodojo.protocol", "task.json must contain a `protocol` object")
        )
    else:
        episodes = protocol.get("episodes")
        if not isinstance(episodes, int) or isinstance(episodes, bool) or episodes <= 0:
            issues.append(
                Issue(
                    "robodojo.episodes",
                    "protocol.episodes must be a positive integer",
                )
            )
    return issues


def _read_manifest(
    package: TaskPackage, name: str, issues: list[Issue]
) -> Mapping[str, Any] | None:
    stem = name.removesuffix(".json")
    try:
        path = package.path(name)
    except TaskPackageError as exc:
        issues.append(Issue(f"axis.{stem}_escapes", str(exc)))
        return None
    if not path.is_file():
        issues.append(Issue(f"axis.{stem}_missing", f"missing {name}"))
        return None
    try:
        value = read_json(path)
    except TaskPackageError as exc:
        issues.append(Issue(f"axis.{stem}_invalid", str(exc)))
        return None
    if not isinstance(value, Mapping):
        issues.append(Issue(f"axis.{stem}_not_object", f"{name} must be an object"))
        return None
    return value


def _payload_of(
    package: TaskPackage, variant: Mapping[str, Any]
) -> Mapping[str, Any] | None:
    relative = variant.get("payload_path")
    if not isinstance(relative, str) or not relative:
        return None
    try:
        value = read_json(package.path(relative))
    except TaskPackageError:
        return None
    return value if isinstance(value, Mapping) else None


def _inside_package(package: TaskPackage, relative: str) -> bool:
    try:
        return package.path(relative).is_file()
    except TaskPackageError:
        return False


__all__ = [
    "AXIS_CHECKER_TYPES",
    "ROBODOJO_PINNED_REF",
    "SCHEMA_VERSION",
    "TASK_FILE",
    "TASK_SOURCES",
    "Issue",
    "PackageFetchError",
    "TaskPackage",
    "TaskPackageError",
    "canonical_json_sha256",
    "checker_types_in",
    "digest_available",
    "digest_of_files",
    "digest_source",
    "discover_packages",
    "fetch_package",
    "load_package",
    "package_digest",
    "package_files",
    "read_json",
    "select_package",
    "validate_package",
]
