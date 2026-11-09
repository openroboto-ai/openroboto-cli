"""``openroboto task`` -- inspect and validate a task package before submission.

A task season takes a **task package** (``task.json`` + instances) instead of a
model checkpoint. The miner needs two answers before paying the entry fee:

1. **What will the subnet call this package?** The digest the validator reports
   and the backend stores in ``tasks.package_digest``. It is computed from the
   repository contents, not from the directory name, and this command prints the
   exact per-file hashes that make it up so a mismatch can be diffed.
2. **Is the package well formed for the declared source?** Required manifests,
   instance references and the canonical hashes the manifests declare.

What this command does **not** claim: that the task is solvable, deterministic
or not a freebie. Those are the validator's runtime admission
(``task_source/admission.py``); a static check cannot answer them, and saying it
did would be the same lie as a mocked commitment.

``--repo/--commit`` downloads the repository at that exact revision (read-only)
and judges what the validator will read. Without them the local directory is
judged -- useful before the upload, but the fee buys a verdict on the
repository, so the remote form is the one to run before paying.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import tempfile
from typing import Any

from openroboto.backend_api import (
    BackendError,
    fetch_liveness,
    fetch_my_tasks,
    fetch_readiness,
    fetch_task_access_message,
    miner_key,
)
from openroboto.chain import hotkey_signature as sign_hotkey
from openroboto.chain import open_wallet
from openroboto.commands.task_authoring import add_authoring_parsers
from openroboto.commands.task_scene import add_parser as add_scene_parser
from openroboto.config import ConfigError, Settings
from openroboto.console import fail, say
from openroboto.task_package import (
    TASK_SOURCES,
    PackageFetchError,
    TaskPackageError,
    digest_source,
    fetch_package,
    load_package,
    package_digest,
    package_files,
    validate_package,
)
from openroboto.task_package import (
    select_package as select_package_dir,
)


def add_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "task",
        help="inspect and validate a task package (task-authoring seasons)",
    )
    subparsers_ = parser.add_subparsers(dest="task_command")
    add_authoring_parsers(subparsers_)
    add_scene_parser(subparsers_)
    check = subparsers_.add_parser(
        "check",
        help="print per-file SHA-256s and the package digest; validate the format",
    )
    check.add_argument(
        "path",
        nargs="?",
        default=".",
        help="local task package directory, defaults to the current directory",
    )
    check.add_argument(
        "--repo",
        default="",
        help="HuggingFace repository id to download and judge instead of `path`",
    )
    check.add_argument(
        "--commit",
        default="",
        help="full 40-character commit for --repo (not a branch or tag)",
    )
    check.add_argument(
        "--package",
        default="",
        help="package directory inside `path`/`--repo` (relative), when the "
        "root holds more than one and the name alone does not pick it",
    )
    check.add_argument(
        "--source",
        default="",
        choices=("", *TASK_SOURCES),
        help="disambiguate when the repository holds packages for more than one "
        "source (the validator also filters by the season's source)",
    )
    check.add_argument(
        "--config",
        default="miner.yaml",
        help="read huggingface.token from this file when fetching --repo "
        "(default: miner.yaml; never required for a public package)",
    )
    check.add_argument(
        "--expect-digest",
        default="",
        help="fail when the computed digest differs from this value (for example "
        "the validator's reported package_digest)",
    )
    check.add_argument(
        "--json",
        action="store_true",
        help="machine-readable output (one JSON object on stdout)",
    )
    check.set_defaults(handler=run)

    status = subparsers_.add_parser(
        "status",
        help="read the task packages this hotkey submitted (MINER key + signature)",
    )
    status.add_argument("--config", default="miner.yaml")
    status.add_argument(
        "--backend-url",
        default="",
        help="override backend.url from --config; useful for the isolated "
        "instance (http://127.0.0.1:8091) without rewriting the file",
    )
    status.add_argument(
        "--hotkey",
        default="",
        help="SS58 whose packages to read; defaults to subnet.hotkey_ss58 or the "
        "wallet's own hotkey",
    )
    status.add_argument("--status", default="", help="filter by task status")
    status.add_argument("--source", default="", choices=("", *TASK_SOURCES))
    status.add_argument("--skill", default="")
    status.add_argument("--limit", type=int, default=20)
    status.add_argument(
        "--run-id",
        default="",
        help="operator run id recorded in the output for evidence. Miner task "
        "endpoints have no run_id parameter or header; this is not sent on the "
        "wire (unknown query parameters are rejected by the backend)",
    )
    status.add_argument(
        "--probe",
        action="store_true",
        help="only check GET /healthz and GET /readyz against backend.url; "
        "sends no key and no task query",
    )
    status.add_argument("--json", action="store_true")
    status.set_defaults(handler=status_run)
    parser.set_defaults(handler=lambda args: _usage(parser))


def _usage(parser: argparse.ArgumentParser) -> int:
    parser.print_help()
    return 1


def status_run(args: argparse.Namespace) -> int:
    """Read-only: `/healthz` + `/readyz`, or this hotkey's own task packages."""
    settings = _task_settings(args.config)
    if args.backend_url:
        settings.backend_url = args.backend_url
    run_id = str(getattr(args, "run_id", "") or "")
    if args.probe:
        return _probe(settings, as_json=args.json, run_id=run_id)
    if not settings.backend_url:
        fail(
            f"{args.config} does not name a backend (`backend.url`), so there "
            f"is nowhere to ask.\n"
            f"   → set backend.url (for the isolated run: http://127.0.0.1:8091)"
        )
        return 1

    wallet = open_wallet(settings)
    wallet_hotkey = str(wallet.hotkey.ss58_address)
    hotkey = args.hotkey or settings.hotkey_ss58 or wallet_hotkey
    if hotkey != wallet_hotkey:
        # The signature is the identity proof; an address whose key is not in
        # this wallet cannot produce it, and the backend answers 401 without
        # saying which of the two addresses was wrong.
        fail(
            f"The wallet in {args.config} holds hotkey {wallet_hotkey}, but "
            f"{hotkey} was named for this query. Only that hotkey's own private "
            f"key can sign the access message.\n"
            f"   → put {hotkey} in the wallet this config names "
            f"(`subnet.hotkey`), or query from the workspace whose wallet holds "
            f"it"
        )
        return 1

    key = miner_key(settings)
    try:
        message = fetch_task_access_message(settings.backend_url, hotkey)
        signature = sign_hotkey(wallet, message.encode("utf-8"))
        envelope = fetch_my_tasks(
            settings.backend_url,
            hotkey=hotkey,
            signature=signature,
            miner_key=key,
            limit=args.limit,
            status=args.status,
            source=args.source,
            skill=args.skill,
        )
    except BackendError as exc:
        _task_error(exc)
        return 1

    if args.json:
        say(
            json.dumps(
                {
                    "backend": settings.backend_url,
                    "hotkey": hotkey,
                    "miner_key_present": bool(key),
                    "request_id": envelope.meta.request_id,
                    "run_id": run_id,
                    "tasks": [row.model_dump(mode="json") for row in envelope.data],
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    say(f"backend:    {settings.backend_url}")
    say(f"hotkey:     {hotkey}")
    say(f"request_id: {envelope.meta.request_id}")
    if run_id:
        say(f"run_id:     {run_id}")
    if not envelope.data:
        say("no task packages for this hotkey")
        return 0
    for row in envelope.data:
        say(f"{row.task_id}  {row.status}  {row.source or '?'}/{row.skill or '-'}")
        say(f"   package: {row.package_repo}@{row.package_commit}")
        say(f"   digest:  {row.package_digest or '(not reported yet)'}")
        eligible = (
            "unknown"
            if row.settlement_eligible is None
            else "yes"
            if row.settlement_eligible
            else "no"
        )
        say(
            f"   reward (backend): D={_reported_score(row.discrimination)}"
            f"  H(current)={_reported_score(row.effective_hardness)}"
            f"  S={_reported_score(row.score)}  eligible={eligible}"
        )
        say(f"   H(classified): {_reported_score(row.hardness)}")
        say(f"   hard proof: {row.hardness_proof_hash or '(not reported)'}")
        if row.content_sha256:
            say(f"   content: {row.content_sha256}")
        if row.reject_reason:
            say(f"   reject:  {row.reject_reason}")
        if row.visibility or row.content_state:
            say(
                f"   archive: {row.content_state or '?'}"
                f"{' / ' + row.visibility if row.visibility else ''}"
            )
    return 0


def _reported_score(value: float | None) -> str:
    """Format only a backend-reported score; never infer missing rewards."""
    return "unknown" if value is None else f"{value:.6g}"


def _probe(settings: Settings, *, as_json: bool, run_id: str = "") -> int:
    """`/healthz` and `/readyz` only. No key, no signature, no writes."""
    if not settings.backend_url:
        fail(
            "No backend.url in the config, so there is nothing to probe.\n"
            "   → for the isolated run set backend.url: http://127.0.0.1:8091"
        )
        return 1
    try:
        live = fetch_liveness(settings.backend_url)
        ready = fetch_readiness(settings.backend_url)
    except BackendError as exc:
        fail(str(exc))
        return 1

    if as_json:
        say(
            json.dumps(
                {
                    "backend": settings.backend_url,
                    "run_id": run_id,
                    "liveness": live.model_dump(mode="json"),
                    "readiness": ready.model_dump(mode="json"),
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 0 if ready.ready else 1

    say(f"backend:  {settings.backend_url}")
    say(
        f"healthz:  {live.status} "
        f"(competition={live.competition}, netuid={live.netuid})"
    )
    say(f"readyz:   {'ready' if ready.ready else 'NOT ready'}")
    say(
        f"database: {'ok' if ready.database.ok else 'FAILED'}"
        f"{' -- ' + ready.database.detail if ready.database.detail else ''}"
    )
    say(
        f"migration:{'ok' if ready.migration.ok else 'FAILED'}"
        f"{' -- ' + ready.migration.detail if ready.migration.detail else ''}"
    )
    if ready.expected_head or ready.alembic_version:
        say(
            f"   alembic: {ready.alembic_version or '(not read)'} "
            f"(expected {ready.expected_head or '(not compared)'})"
        )
    return 0 if ready.ready else 1


def _task_error(exc: BackendError) -> None:
    """Translate the two 401s a task query can produce into an action.

    They are different failures and have different fixes: the MINER-level key
    is a rate-limit token in `miner.yaml`, while the signature is the identity
    proof made by the wallet's hotkey. Collapsing them into "unauthorized"
    sends the miner to the wrong file.
    """
    if exc.code == "UNAUTHORIZED":
        fail(
            f"{exc}\n"
            f"   The task endpoints need the **MINER-level** key: set "
            f"`backend.public_key` in miner.yaml from the MINER key the backend "
            f"publishes (or `urls.control_json`).\n"
            f"   Do **not** put the ADMIN_KEY in a miner workspace: it configures "
            f"seasons and money, and this CLI has no field for it."
        )
    elif exc.code == "INVALID_SIGNATURE":
        fail(
            f"{exc}\n"
            f"   The signature must be over the exact text "
            f"`GET /api/v1/tasks/access-message` returns, made by the wallet "
            f"hotkey that `subnet.hotkey` names. Check that `subnet.hotkey` and "
            f"`subnet.hotkey_ss58` point at the same key."
        )
    else:
        fail(str(exc))


def _task_settings(config_path: str) -> Settings:
    path = pathlib.Path(config_path)
    if not path.is_file():
        raise ConfigError(
            f"{config_path} does not exist, so there is no backend to talk to.\n"
            f"   → pass --config <path>, or create a miner.yaml with "
            f"`backend.url` (the isolated run is http://127.0.0.1:8091)"
        )
    return Settings.load(str(path))


def run(args: argparse.Namespace) -> int:
    if bool(args.repo) != bool(args.commit):
        fail("--repo and --commit must be given together")
        return 1

    temporary: tempfile.TemporaryDirectory[str] | None = None
    try:
        if args.repo:
            temporary = tempfile.TemporaryDirectory(prefix="openroboto-task-")
            try:
                directory = fetch_package(
                    args.repo,
                    args.commit,
                    token=_token(args.config),
                    cache_dir=temporary.name,
                )
            except PackageFetchError as exc:
                fail(
                    f"Could not fetch {args.repo}@{args.commit[:8]}.\n   {exc}\n"
                    f"   Nothing was uploaded, paid or sent on chain."
                )
                return 1
            where = f"{args.repo}@{args.commit}"
            snapshot_root = directory
            selected = select_package(
                snapshot_root,
                source=args.source,
                relative=args.package,
                where=where,
            )
            if selected is None:
                return 1
            directory = selected
        else:
            snapshot_root = pathlib.Path(args.path)
            selected = select_package(
                snapshot_root,
                source=args.source,
                relative=args.package,
                where=str(snapshot_root),
            )
            if selected is None:
                return 1
            directory = selected
            where = str(directory)

        summary = inspect(directory)
    finally:
        if temporary is not None:
            temporary.cleanup()

    summary["locator"] = where
    summary["repo"] = args.repo
    summary["commit"] = args.commit
    if snapshot_root.resolve() != directory.resolve():
        # The package is not the repository root. Print both so a digest
        # mismatch can be traced to the directory the validator will copy.
        summary["snapshot_root"] = str(snapshot_root)
        summary["package_dir"] = str(directory)

    if args.expect_digest:
        expected = args.expect_digest.strip().lower()
        summary["expected_digest"] = expected
        summary["digest_matches"] = expected == summary["package_digest"]
        if not summary["digest_matches"]:
            summary["ok"] = False

    if args.json:
        say(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        _print_human(summary)
    return 0 if summary["ok"] else 1


def select_package(
    root: pathlib.Path,
    *,
    source: str,
    relative: str,
    where: str,
) -> pathlib.Path | None:
    """Pick the package directory the validator would copy and digest.

    Thin printing wrapper around ``task_package.select_package`` so the command
    and the pre-payment gate in ``submit`` cannot choose differently.
    """
    if not root.is_dir():
        # `inspect` owns the "not a directory" verdict, including the JSON
        # form; discovering inside a missing directory would only hide it.
        return root
    try:
        return select_package_dir(root, source=source, relative=relative)
    except TaskPackageError as exc:
        fail(f"{exc}\n   Nothing was uploaded, paid or sent on chain.")
        return None


def inspect(directory: str | pathlib.Path) -> dict[str, Any]:
    """Everything both the human and JSON outputs need, without printing."""
    root = pathlib.Path(directory).expanduser()
    summary: dict[str, Any] = {
        "ok": False,
        "path": str(root),
        "source": "",
        "task_id": "",
        "instruction": "",
        "package_digest": "",
        "digest_source": digest_source(),
        "file_count": 0,
        "total_bytes": 0,
        "files": [],
        "issues": [],
    }
    if not root.is_dir():
        summary["issues"] = [
            {
                "code": "package.directory_missing",
                "message": f"{root} is not a directory",
                "severity": "error",
            }
        ]
        return summary

    try:
        files = package_files(root)
        summary["package_digest"] = package_digest(root)
    except TaskPackageError as exc:
        summary["issues"].append(
            {
                "code": "package.digest_unavailable",
                "message": str(exc),
                "severity": "error",
            }
        )
        return summary
    summary["file_count"] = len(files)
    summary["files"] = [
        {
            "path": relative,
            "sha256": digest,
            "size": (root / relative).stat().st_size,
        }
        for relative, digest in files
    ]
    summary["total_bytes"] = sum(item["size"] for item in summary["files"])
    if any(item["path"] == "run_info.json" for item in summary["files"]):
        # The uploader deliberately does not write this into a task package
        # (it would change the digest). A leftover from a model upload still
        # counts, because the validator digests every file.
        summary["issues"].append(
            {
                "code": "package.run_info_present",
                "message": (
                    "run_info.json is part of the package and therefore part of "
                    "the digest. The task uploader does not add it; a leftover "
                    "from a model workspace changes the package identity. "
                    "Remove it if it is not task content."
                ),
                "severity": "warning",
            }
        )

    try:
        package = load_package(root)
    except TaskPackageError as exc:
        summary["issues"].append(
            {"code": "package.invalid", "message": str(exc), "severity": "error"}
        )
        return summary

    summary["source"] = package.source
    summary["task_id"] = package.task_id
    summary["instruction"] = package.instruction
    issues = validate_package(package)
    summary["issues"].extend(
        {"code": issue.code, "message": issue.message, "severity": issue.severity}
        for issue in issues
    )
    summary["ok"] = not any(issue.is_error for issue in issues)
    return summary


def _print_human(summary: dict[str, Any]) -> None:
    say(f"task package: {summary['locator']}")
    say(f"directory:    {summary['path']}")
    if summary["source"]:
        say(f"source:       {summary['source']}")
        say(f"task_id:      {summary['task_id']}")
        say(f"instruction:  {summary['instruction']}")
    say(f"files:        {summary['file_count']}")
    say(f"package_digest: {summary['package_digest']}")
    say(f"digest_source:  {summary['digest_source']}")
    say("")
    for item in summary["files"]:
        say(f"  {item['sha256']}  {item['path']}")

    if "expected_digest" in summary:
        verdict = "matches" if summary["digest_matches"] else "DOES NOT MATCH"
        say("")
        say(f"expected digest: {summary['expected_digest']} ({verdict})")
        if not summary["digest_matches"]:
            summary["ok"] = False

    errors = [item for item in summary["issues"] if item["severity"] == "error"]
    warnings = [item for item in summary["issues"] if item["severity"] != "error"]
    for item in warnings:
        say(f"⚠️  [{item['code']}] {item['message']}")
    for item in errors:
        say(f"❌ [{item['code']}] {item['message']}")

    say("")
    if errors or not summary["ok"]:
        say("❌ package is not valid; do not submit it")
    elif warnings:
        say("✅ package format is valid (warnings above still need reading)")
    else:
        say("✅ package format is valid")
    say(
        "   Runtime admission (solvable / not a freebie / determinism) is the "
        "validator's job and is not claimed here."
    )


def _token(config_path: str) -> str:
    """The HF token from the workspace config, if there is one.

    A public task package needs no token at all. A gated one does, but the
    command must not require a `miner.yaml` just to inspect a local directory;
    an unreadable config is treated as "no token" and the fetch error carries
    the real reason.
    """
    path = pathlib.Path(config_path)
    if not path.is_file():
        return ""
    try:
        return Settings.load(str(path)).hf_token
    except ConfigError:
        return ""


__all__ = ["add_parser", "inspect", "run"]
