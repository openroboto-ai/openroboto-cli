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

from openroboto.config import ConfigError, Settings
from openroboto.console import fail, say
from openroboto.task_package import (
    TASK_SOURCES,
    PackageFetchError,
    TaskPackageError,
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
        help="package directory inside --repo (relative path), when the "
        "repository holds more than one and the name alone does not pick it",
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
    parser.set_defaults(handler=lambda args: _usage(parser))


def _usage(parser: argparse.ArgumentParser) -> int:
    parser.print_help()
    return 1


def run(args: argparse.Namespace) -> int:
    if bool(args.repo) != bool(args.commit):
        fail("--repo and --commit must be given together")
        return 1
    if args.package and not args.repo:
        fail(
            "--package selects a directory inside --repo; pass the directory "
            "as the positional `path` for a local package"
        )
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
                relative="",
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

    files = package_files(root)
    summary["package_digest"] = package_digest(root)
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

    try:
        package = load_package(root)
    except TaskPackageError as exc:
        summary["issues"] = [
            {"code": "package.invalid", "message": str(exc), "severity": "error"}
        ]
        return summary

    summary["source"] = package.source
    summary["task_id"] = package.task_id
    summary["instruction"] = package.instruction
    issues = validate_package(package)
    summary["issues"] = [
        {"code": issue.code, "message": issue.message, "severity": issue.severity}
        for issue in issues
    ]
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
