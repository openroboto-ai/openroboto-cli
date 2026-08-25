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

Why a warning also stops you here
---------------------------------
The protocol package splits its findings in two: `errors` are what admission
rejects, `warnings` are the cases admission **accepts** and the evaluator then
cannot load. This command exits non-zero on both, which is deliberately
stricter than the backend, because the two sides are answering different
questions. Admission asks "does this submission count"; this command asks
"will the money you are about to spend buy you a score". A submission that is
admitted and then fails at evaluation is the more expensive outcome of the
two: the TAO is already burned, the queue slot is already used, and there is
nothing left to fix it with.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from openroboto_protocol.model_format import (
    LIBERO_LAYOUT,
    CheckpointFile,
    FormatIssueCode,
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


def weights_subdir(directory: Path) -> str:
    """Where the weights actually sit, relative to `directory` (`""` when they
    are already at the top).

    Only used to turn "nested too deep" into a path the miner can copy. The
    shallowest hit wins, matching the evaluator: it takes the first checkpoint
    it finds while descending.
    """
    roots = [
        path.parent
        for path in directory.rglob("*")
        if path.is_file() and path.name.endswith((".safetensors", ".bin"))
    ]
    roots += [
        path.parent
        for path in directory.rglob(LIBERO_LAYOUT.jax_params_dir)
        if path.is_dir()
    ]
    if not roots:
        return ""
    closest = min(roots, key=lambda path: len(path.relative_to(directory).parts))
    return closest.relative_to(directory).as_posix().removeprefix(".")


def nesting_advice(directory: Path) -> list[str]:
    """The two ways out of `nested_too_deep`, with the miner's own paths filled
    in.

    "Your layout is invalid" is not something anyone can act on; "upload this
    directory instead" is. The layout is also not the miner's invention -- the
    vendor's own post-trained artifact ships its checkpoint under
    `checkpoints/global_step_N/hf_ckpt/`, so a miner who uploads the training
    output unchanged is copying the published example.
    """
    subdir = weights_subdir(directory)
    if not subdir:
        return []
    return [
        f"   → Your weights are in: {subdir}/",
        "     That is below the depth the evaluator searches. The official LingBot",
        "     artifact is laid out this way too, so uploading the training output",
        "     unchanged is the normal way to end up here.",
        "     Upload that directory as the repository root instead:",
        f"       openroboto check {directory / subdir}",
        f"       openroboto submit --output-dir {directory / subdir}",
        "     Or move everything inside it up to the top of the checkpoint directory.",
    ]


def report_result(directory: Path, report: FormatReport) -> int:
    """Print the verdict. Returns the exit code: 0 = fine to submit."""
    say(f"checkpoint: {directory}")
    say(f"weights: {report.kind.value if report.kind else 'unrecognized'}")
    say(f"counted size: {report.counted_size_bytes / 1024 / 1024:.1f} MB")

    for warning in report.warnings:
        say(f"⚠️  [{warning.code.value}] {warning.message}")
        if warning.code == FormatIssueCode.NESTED_TOO_DEEP:
            for line in nesting_advice(directory):
                say(line)

    for error in report.errors:
        say(f"❌ [{error.code.value}] {error.message}")

    if report.errors or report.warnings:
        say("")
        if not report.errors:
            # Spelling out *why* a green admission verdict is still a stop:
            # otherwise the natural reading of "the backend accepts it" is
            # "submit anyway", which is precisely the run that wastes the TAO.
            say(
                "The subnet would accept this upload -- it is the evaluator "
                "that cannot load it."
            )
            say(
                "That is worse than being rejected: by the time it fails, the "
                "TAO is burned and the queue slot is used."
            )
        say("→ Do not burn yet. Fix the above, then run `openroboto check` again;")
        say("  the burn behind a submission that fails is not refunded.")
        return 1

    say("✅ Format check passed, you can run `openroboto submit`")
    return 0
