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

Which rules, though, is this module's job
-----------------------------------------
The subnet runs more than one competition, and a checkpoint that is perfect for
one is unloadable rubbish for another: π0.5 (openpi) wants a `model.safetensors`
next to `assets/.../norm_stats.json`, LingBot-VLA 2.0 wants sharded safetensors
next to a `model.safetensors.index.json`. The rule book is chosen from the
competition in `miner.yaml` -- never sniffed from the directory, because
sniffing is guessing and a wrong guess is delivered to the miner as "your upload
is broken" seconds before they decide whether to pay.

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
import json
from collections.abc import Mapping
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from openroboto_protocol import model_format
from openroboto_protocol.model_format import (
    LIBERO_LAYOUT,
    CheckpointFile,
    FormatIssueCode,
    FormatReport,
    check_checkpoint_layout,
)

from openroboto import adapters
from openroboto.config import ConfigError, Settings
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
    parser.add_argument("--config", default="miner.yaml")
    parser.set_defaults(handler=run)


def run(args: argparse.Namespace) -> int:
    directory = Path(args.path or resolve_output_dir(resolve_round(args.round)))
    if not directory.is_dir():
        say(f"❌ Directory does not exist: {directory}")
        return 1

    layout = resolve_layout(competition_settings(args.config))
    report = check_directory(directory, layout=layout)
    return report_result(directory, report, layout=layout)


def competition_settings(path: str) -> Settings:
    """Read `miner.yaml` for the competition, tolerating its absence.

    `check` is free, local, and the documented fix for a too-deeply nested
    upload is to point it at a subdirectory -- so it has to keep working in a
    directory that holds no config at all. No config = no competition = the π0.5
    rules, which is exactly what this command did before competitions existed.

    That default is safe in the direction that matters: a LingBot checkpoint
    judged by the π0.5 rules is *rejected*, never waved through, so a miner who
    runs this from the wrong directory loses a minute, not their burn. The
    `rules:` line in the report says which book was used.
    """
    return Settings.load(path) if Path(path).is_file() else Settings()


def resolve_layout(settings: Settings) -> Any | None:
    """The competition's LingBot layout, or `None` for the π0.5 (openpi) rules.

    `None` is what every config without a competition section resolves to, so
    upgrading the CLI does not change one verdict for a miner who changed
    nothing.
    """
    if adapters.format_profile(settings.competition_adapter) == adapters.OPENPI:
        return None
    return lingbot_layout(settings.competition_params)


def lingbot_layout(params: Mapping[str, Any]) -> Any:
    """Build this competition's `LingbotLayout` out of `competition.params`.

    The protocol package deliberately publishes **no** singleton for this class:
    three of its five fields are competition parameters, and freezing one
    season's camera count into a published package means a release plus a
    fleet-wide upgrade to change a number. So they come from the config, and
    each missing key falls back to the package's own constant -- never to a
    string spelled out here, which is how two copies of a contract start
    drifting.

    `cli_config_file` falls back to `None` (rule off) rather than to a file
    name: the vendor's own base checkpoint does not contain the descriptor and
    the export path never writes one, so requiring it by default would reject
    every LingBot submission including the vendor's, after the TAO was burned.

    🔴 **The backend builds its layout from the same row, the same way**
    (`app/domain/hf_layout.py::lingbot_layout`, 2026-08-26). Until that day it
    used the package constants for all five fields and read nothing from the
    competition, which agreed with this function only because nobody had filled
    in `format.model_config_file` / `weights_index_file` / `cli_config_file` --
    and those are exactly the three keys `check_lingbot_layout` reads. The first
    competition to set one would have gone green here and been **rejected
    terminally after payment** there. The two sides are pinned equal field by
    field by the backend's `tests/domain/test_lingbot_layout_single_source.py`;
    **changing the lookup here without changing it there turns that test red.**

    A key present but null falls back too (`or`, not `get(key, default)`):
    migration 0007 seeds the keys it does not know yet as SQL `NULL`, and null
    means "this competition does not constrain it" everywhere else in `params`.
    Handing `None` to a `str` field would be a crash rather than a fallback.
    """
    fmt = params.get("format") or {}
    layout_cls = protocol_rule("LingbotLayout")
    return layout_cls(
        model_config_file=fmt.get("model_config_file")
        or protocol_rule("LINGBOT_MODEL_CONFIG_FILE"),
        weights_index_file=fmt.get("weights_index_file")
        or protocol_rule("LINGBOT_WEIGHTS_INDEX_FILE"),
        camera_names=tuple(fmt.get("cameras") or ()),
        joint_field_names=tuple(fmt.get("joints") or ()),
        cli_config_file=fmt.get("cli_config_file") or None,
    )


def protocol_rule(name: str) -> Any:
    """One rule out of the protocol package, or refuse to check at all.

    **Capability detection, not a version comparison.** `pyproject.toml` pins the
    protocol package exactly, but `openroboto doctor` exists precisely because a
    real environment can still hold a different build, and what decides whether
    the LingBot rules can run is whether they are *there* -- not what a version
    string claims.

    🔴 The one thing this must never do is fall back to the π0.5 rules. Those
    report `missing_weights` for a flawless LingBot checkpoint, which reads to
    the miner as "my upload is broken" at the exact moment they are deciding
    whether to spend, and it hides the nesting warning that is the expensive one.
    A verdict from the wrong rule book is worse than no verdict: no verdict stops
    them, a wrong one sends them to fix something that was never wrong.
    """
    rule = getattr(model_format, name, None)
    if rule is None:
        raise ConfigError(
            f"This workspace mines a competition judged by the LingBot-VLA layout "
            f"rules, and the protocol package installed here "
            f"({protocol_version()}) does not carry them.\n"
            f"  → pip install -U openroboto\n"
            f"  (`openroboto --version` prints both versions; the client pins the "
            f"protocol package it was built against.)\n"
            f"  Not falling back to the π0.5 (openpi) rules on purpose: they "
            f"report 'no model weights found' for a perfectly good LingBot "
            f"checkpoint. Being told nothing costs you a command; being told the "
            f"wrong thing costs you a burn."
        )
    return rule


def protocol_version() -> str:
    """Version of the installed protocol package, for the refusal above."""
    try:
        return f"openroboto-protocol {version('openroboto-protocol')}"
    except PackageNotFoundError:  # pragma: no cover -- a normal install has it
        return "openroboto-protocol, not installed"


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


def check_directory(directory: Path, *, layout: Any = None) -> FormatReport:
    """Hand the file list to the rule book this competition uses.

    Not one format rule lives in this repository: two copies of the admission
    contract drift, and the miner is the one who pays for the difference.
    """
    files = collect_files(directory)
    if layout is None:
        return check_checkpoint_layout(files)
    report: FormatReport = protocol_rule("check_lingbot_layout")(
        files, layout, weight_map=read_weight_map(directory, layout)
    )
    return report


def read_weight_map(directory: Path, layout: Any) -> Mapping[str, str] | None:
    """Parse `model.safetensors.index.json` -- the `{tensor: shard}` map.

    Reading it is what makes this command **stricter than admission**, and the
    reason to want that is not the one this docstring used to give. It claimed
    "the backend, which can read the same file, does" -- **it does not.**
    `judge_lingbot_tree` passes `weight_map=None` and says why in so many words:
    the backend's rule book lives in its domain layer, which performs zero I/O,
    so the missing-shard and missing-tensor rules are not evaluated there at all
    ("absence of evidence, not evidence of a missing shard").

    So a broken index does not get you rejected after burning. It gets you
    *admitted* after burning, and then the evaluator cannot load the model --
    which by this module's own header is the more expensive of the two. This
    command is the only place it can be caught before the money moves.

    The direction is the safe one (stricter before payment, never looser), but
    the sentence had to go: believing the backend already checks this is how a
    real gap stops getting closed.

    It does not break the "never download the weights" rule either: the index is
    a few hundred KB of plain text sitting next to the shards, not the shards.

    Unreadable or missing → `None` plus a printed line. Silence would be the bad
    kind of lenient: two rules quietly not running, and a green check that means
    less than the miner thinks it does.
    """
    found = sorted(directory.rglob(layout.weights_index_file), key=_depth)
    if not found:
        return None
    try:
        weight_map = json.loads(found[0].read_text(encoding="utf-8"))["weight_map"]
    except (OSError, ValueError, KeyError, TypeError):
        weight_map = None
    if not isinstance(weight_map, dict):
        say(
            f"⚠️  could not read the weight index {found[0]} — the shard and "
            "tensor rules were not checked. The subnet's admission does not "
            "read it either, so nothing checks them before you pay: a shard "
            "listed here but missing from the repo is found by the evaluator, "
            "after the burn."
        )
        return None
    return weight_map


def _depth(path: Path) -> int:
    return len(path.parts)


def weights_subdir(directory: Path) -> str | None:
    """Where the weights actually sit, relative to `directory`.

    Three answers, and the miner needs all three kept apart:
    `""` they are already at the top, `"a/b"` they are in that subdirectory,
    `None` there are no weight files anywhere under `directory` at all.

    The last one used to be `""` as well, which was harmless while the only
    caller was the nesting advice below. It is not harmless for `openroboto
    train`, which uses this to tell "your trainer nested the checkpoint" apart
    from "nothing exported a checkpoint" -- two different mistakes with two
    different fixes.

    The shallowest hit wins, matching the evaluator: it takes the first
    checkpoint it finds while descending.
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
        return None
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


def report_result(directory: Path, report: FormatReport, *, layout: Any = None) -> int:
    """Print the verdict. Returns the exit code: 0 = fine to submit."""
    say(f"checkpoint: {directory}")
    # Which rule book judged this is part of the verdict, not decoration: the
    # failure this line exists to make visible is a LingBot checkpoint being told
    # it has "no model weights" by the π0.5 rules, which looks like a broken
    # upload and is really a misrouted check.
    say(f"rules: {'π0.5 (openpi)' if layout is None else 'LingBot-VLA 2.0'}")
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
