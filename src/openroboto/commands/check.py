"""`openroboto check` -- decide locally, before paying, whether a checkpoint
can be evaluated.

This step used to require cloning a second repository
(`openroboto-evaluation`'s `libero_eval/check_model.py`), and the actual
outcome was that nobody ran it -- which made "finding out only after burning
the TAO that what was uploaded is a bare LoRA adapter" the most common way to
burn for nothing.

The decision rules **are not implemented here**: it calls
`openroboto_protocol.model_format`, the same code and the same set of error
codes the backend uses for admission. Purely local, zero GPU, zero network.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from openroboto_protocol.model_format import (
    CheckpointFile,
    FormatReport,
    check_checkpoint_layout,
)

from openroboto.console import say
from openroboto.round_state import resolve_output_dir, resolve_round


def add_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "check",
        help="Validate the model format locally before paying (the same rules the "
        "evaluator uses)",
    )
    parser.add_argument(
        "path",
        nargs="?",
        default="",
        help="checkpoint directory, defaults to this round's training output directory",
    )
    parser.add_argument(
        "--round", type=int, default=0, help="round number, auto-detected by default"
    )
    parser.set_defaults(handler=run)


def run(args: argparse.Namespace) -> int:
    directory = Path(args.path or resolve_output_dir(resolve_round(args.round)))
    if not directory.is_dir():
        say(f"❌ Directory does not exist: {directory}")
        return 1

    report = check_directory(directory)
    return report_result(directory, report)


def collect_files(directory: Path) -> list[CheckpointFile]:
    """List the files in the directory as the inventory the protocol package
    wants (relative POSIX path + byte count)."""
    return [
        CheckpointFile(
            path=path.relative_to(directory).as_posix(), size_bytes=path.stat().st_size
        )
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    ]


def check_directory(directory: Path) -> FormatReport:
    return check_checkpoint_layout(collect_files(directory))


def report_result(directory: Path, report: FormatReport) -> int:
    """Print the verdict. Returns the exit code: 0 = fine to submit."""
    say(f"checkpoint: {directory}")
    say(f"weights: {report.kind.value if report.kind else 'unrecognized'}")
    say(f"counted size: {report.counted_size_bytes / 1024 / 1024:.1f} MB")

    for warning in report.warnings:
        say(f"⚠️  [{warning.code.value}] {warning.message}")

    if report.ok:
        say("✅ Format check passed, you can run `openroboto submit`")
        return 0

    for error in report.errors:
        say(f"❌ [{error.code.value}] {error.message}")
    say("")
    say(
        "→ Do not burn yet. Fix these, then run `openroboto check` again; "
        "the burn behind a rejected submission is not refunded."
    )
    return 1
