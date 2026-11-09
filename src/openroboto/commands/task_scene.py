"""Explicit scene authoring and registration, preserving existing task defaults."""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import tempfile
from typing import Any

from openroboto.backend_api import (
    BackendError,
    PrivateTask,
    fetch_my_tasks,
    fetch_task_access_message,
    miner_key,
)
from openroboto.chain import hotkey_signature, open_wallet
from openroboto.config import Settings
from openroboto.console import say
from openroboto.scene_api import (
    fetch_scene_progress,
    register_scene,
    registration_message,
)
from openroboto.scene_authoring import run_authoring
from openroboto.scene_package import (
    ScenePackageError,
    build_submission,
    load_submission,
)
from openroboto.task_package import PackageFetchError, fetch_package

REGISTRATION_PAGE_SIZE = 100
REGISTRATION_MAX_PAGES = 100


def add_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("scene", help="opt-in schema-3 scene submissions")
    commands = parser.add_subparsers(dest="scene_command", required=True)
    build = commands.add_parser(
        "build", help="materialize and bind both roots (structure only)"
    )
    build.add_argument("--execution", required=True, type=pathlib.Path)
    build.add_argument("--output", required=True, type=pathlib.Path)
    private = build.add_mutually_exclusive_group(required=True)
    private.add_argument("--reference", type=pathlib.Path)
    private.add_argument("--private", type=pathlib.Path)
    build.set_defaults(handler=run_build)
    check = commands.add_parser("check", help="check dependencies and joint identity")
    check.add_argument("package", nargs="?", default=".")
    check.add_argument("--repo", default="")
    check.add_argument("--commit", default="")
    check.add_argument("--config", default="miner.yaml")
    check.add_argument("--expect-submission-digest", default="")
    check.set_defaults(handler=run_check)
    for name in ("record-reference", "self-test"):
        command = commands.add_parser(
            name, help="run the exact installed scene runtime"
        )
        command.add_argument(
            "package",
            nargs="?",
            help="self-test shorthand; recording needs explicit E and NEW P/manifest",
        )
        command.add_argument("--execution", type=pathlib.Path)
        command.add_argument(
            "--private", type=pathlib.Path, help="NEW private root for recording"
        )
        command.add_argument(
            "--manifest", type=pathlib.Path, help="detached JSON; NEW for recording"
        )
        command.add_argument("--runtime-python", default=sys.executable)
        command.add_argument(
            "--timeout-seconds",
            type=float,
            default=900.0 if name == "record-reference" else 180.0,
            help="total deadline up to 900 seconds; never raises contract budgets",
        )
        command.add_argument(
            "--output",
            required=True,
            type=pathlib.Path,
            help="new projected public report outside E/P",
        )
        command.add_argument(
            "--private-report",
            type=pathlib.Path,
            help="new private report; default: retained secure temporary directory",
        )
        if name == "record-reference":
            command.add_argument(
                "--script",
                required=True,
                type=pathlib.Path,
                help="local author script outside E/P",
            )
            command.add_argument(
                "--max-reference-steps",
                type=int,
                help="optional LOWER local recording cap",
            )
        else:
            command.add_argument("--expect-submission-digest", default="")
        command.set_defaults(handler=run_runtime)
    register = commands.add_parser(
        "register", help="bind an announced task to both roots"
    )
    register.add_argument("task_id", help="backend task id from task status")
    register.add_argument("--package", required=True)
    register.add_argument("--config", default="miner.yaml")
    register.add_argument(
        "--lookup-offset",
        type=int,
        default=0,
        help="resume author lookup at this offset; "
        "each invocation reads at most 100 pages",
    )
    register.set_defaults(handler=run_register)
    status = commands.add_parser(
        "status", help="read server capability/admission/reward separately"
    )
    status.add_argument("task_id")
    status.add_argument("--config", default="miner.yaml")
    status.set_defaults(handler=run_status)


def _emit(value: dict[str, Any]) -> None:
    say(json.dumps(value, sort_keys=True, indent=2, allow_nan=False))


def _error(exc: Exception) -> int:
    finding = (
        exc.finding
        if isinstance(exc, ScenePackageError)
        else {
            "code": "scene_io_error",
            "path": "package",
            "message": "Check file permissions and available disk space; retain failed "
            "inputs.",
        }
    )
    _emit(
        {
            "ok": False,
            "scope": "structure_only",
            "issues": [finding],
            "local_physics": "not_run",
            "server_admission": "not_run",
            "reward_status": "not_evaluated",
        }
    )
    return 1


def run_build(args: argparse.Namespace) -> int:
    try:
        result = build_submission(
            args.execution.expanduser(),
            args.output.expanduser(),
            reference=args.reference.expanduser() if args.reference else None,
            private=args.private.expanduser() if args.private else None,
        )
    except (ScenePackageError, OSError) as exc:
        return _error(exc)
    _emit(result.report())
    return 0


def run_check(args: argparse.Namespace) -> int:
    try:
        if bool(args.repo) != bool(args.commit):
            raise ScenePackageError(
                "locator_incomplete", "repo", "Supply --repo and --commit together."
            )
        with tempfile.TemporaryDirectory(prefix="openroboto-scene-check-") as cache:
            root = args.package
            if args.repo:
                settings = (
                    Settings.load(args.config)
                    if pathlib.Path(args.config).is_file()
                    else Settings()
                )
                root = str(
                    fetch_package(
                        args.repo, args.commit, token=settings.hf_token, cache_dir=cache
                    )
                )
            result = load_submission(root)
            if (
                args.expect_submission_digest
                and args.expect_submission_digest != result.manifest.submission_digest
            ):
                raise ScenePackageError(
                    "submission_drift",
                    "submission_digest",
                    "Compare both roots with the expected identity.",
                )
            _emit(result.report())
    except PackageFetchError:
        return _error(
            ScenePackageError(
                "storage_unavailable",
                "repo",
                "Check repository permissions, token and fixed 40-character commit;"
                " retry without paying.",
            )
        )
    except (ScenePackageError, OSError) as exc:
        return _error(exc)
    return 0


def run_runtime(args: argparse.Namespace) -> int:
    """Run only explicit scene commands after exact metadata and input binding."""
    report = run_authoring(args)
    _emit(report)
    return 0 if report["ok"] else 1


def _backend_error(exc: BackendError) -> int:
    _emit(
        {
            "ok": False,
            "code": exc.code or "BACKEND_UNAVAILABLE",
            "retryable": exc.retryable,
            "message": "Check scene routes, the MINER key and the author wallet. Retry "
            "this command without paying again.",
        }
    )
    return 1


def run_register(args: argparse.Namespace) -> int:
    try:
        local = load_submission(args.package)
        settings = Settings.load(args.config)
        wallet = open_wallet(settings)
        hotkey = str(wallet.hotkey.ss58_address)
        if settings.hotkey_ss58 and settings.hotkey_ss58 != hotkey:
            raise ScenePackageError(
                "wallet_mismatch",
                "config",
                "Use the wallet holding subnet.hotkey_ss58, then retry "
                "registration without paying again.",
            )
        key = miner_key(settings)
        message = fetch_task_access_message(settings.backend_url, hotkey)
        row = _find_submitted(
            settings,
            task_id=args.task_id,
            hotkey=hotkey,
            signature=hotkey_signature(wallet, message.encode()),
            key=key,
            offset=getattr(args, "lookup_offset", 0),
        )
        with tempfile.TemporaryDirectory(prefix="openroboto-scene-register-") as cache:
            root = fetch_package(
                row.package_repo,
                row.package_commit,
                token=settings.hf_token,
                cache_dir=cache,
            )
            remote = load_submission(root)
        if remote.manifest != local.manifest:
            raise ScenePackageError(
                "submission_drift",
                "submission",
                "The announced repo@commit differs from this local submission. "
                "Select its original unchanged bytes; do not pay again.",
            )
        signature = hotkey_signature(
            wallet,
            registration_message(
                row.task_id, row.package_repo, row.package_commit, local.manifest
            ),
        )
        result = register_scene(
            settings.backend_url, row.task_id, local.manifest, signature, key
        )
        _emit(result.model_dump(mode="json"))
    except BackendError as exc:
        return _backend_error(exc)
    except PackageFetchError:
        return _error(
            ScenePackageError(
                "storage_unavailable",
                "repo",
                "Restore access to the announced fixed revision and retry "
                "registration without paying again.",
            )
        )
    except (ScenePackageError, OSError) as exc:
        return _error(exc)
    return 0


def _find_submitted(
    settings: Settings,
    *,
    task_id: str,
    hotkey: str,
    signature: str,
    key: str,
    offset: int = 0,
) -> PrivateTask:
    """Find an exact backend id across a bounded, authenticated author listing.

    Reuse the row's immutable repo@commit; names and list positions never supply
    an identity. An empty/last page ends lookup, while a page cap is resumable.
    """
    if offset < 0:
        raise ScenePackageError(
            "lookup_offset_invalid",
            "lookup_offset",
            "Use an offset of zero or greater.",
        )
    for _ in range(REGISTRATION_MAX_PAGES):
        page = fetch_my_tasks(
            settings.backend_url,
            hotkey=hotkey,
            signature=signature,
            miner_key=key,
            limit=REGISTRATION_PAGE_SIZE,
            offset=offset,
        )
        if page.meta.page.offset != offset or len(page.data) > REGISTRATION_PAGE_SIZE:
            raise BackendError(
                "The author listing returned inconsistent pagination; retry lookup.",
                code="TASK_LOOKUP_PAGE_MISMATCH",
                retryable=True,
            )
        for row in page.data:
            if row.task_id == task_id:
                return row
        if not page.data or not page.meta.page.has_more:
            break
        offset += len(page.data)
    else:
        raise ScenePackageError(
            "task_lookup_limit",
            "lookup_offset",
            f"Lookup reached {REGISTRATION_MAX_PAGES} pages. Retry the same "
            f"registration with --lookup-offset {offset}; do not pay again.",
        )
    raise ScenePackageError(
        "task_not_found",
        "task_id",
        "No matching backend task id in the searched author pages. Wait for chain "
        "ingestion, verify the wallet and backend task id, then retry from offset 0. "
        "Do not pay again.",
    )


def run_status(args: argparse.Namespace) -> int:
    try:
        settings = Settings.load(args.config)
        result = fetch_scene_progress(settings.backend_url, args.task_id)
        _emit(result.model_dump(mode="json"))
    except BackendError as exc:
        return _backend_error(exc)
    return 0
