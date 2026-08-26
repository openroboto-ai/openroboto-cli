"""`openroboto submit` -- upload → check the layout → check the competition →
pay → announce (the old `rt.py submit`).

The steps reuse the very same implementations from the `upload` / `burn` /
`announce` modules. The old `rt.py` **copied all three again** inside
`cmd_submit`, and the self-checks and skip conditions in the two places
gradually grew apart; here there is only one copy.

The checkpoint makes this command naturally re-entrant: what has been uploaded
is not uploaded again, what has been paid for is not paid for again.

## The layout is checked here too, and on the repository

`openroboto check` is a separate command a miner may or may not run, and the
promise "nothing is judged after you have paid" was only ever true for the ones
who did. Everyone else met the layout rules for the first time in the backend's
admission -- which runs **after** the fee, reaches `HF_STRUCTURE_INVALID`, and
files the submission as `rejected`: final, no retry, no refund, while the model
itself may have been perfectly good.

So the gate runs here, between the upload and the payment, for every workspace
that mines a competition. **There is no `--skip-check` and there will not be
one.** A flag like that keeps today's hole and renames it: whoever uses it burns
exactly the TAO this exists to save. If the gate refuses a model that was fine,
the gate is what gets fixed.

⚠️ **A config from before competitions existed does not reach it**, and that is
not the byte-compatibility promise talking. What this gate offers is "the rules
that judge you after the fee ran before it", and for those miners we cannot
offer it: their submissions are judged by the backend's *own* π0.5 reader
(`app/domain/hf_layout.py::judge_hf_tree` with its own `_MODEL_PATTERNS` /
`_REQUIRED_ASSETS`), not by the protocol package, so anything decided here would
be a guess at another implementation's verdict -- and a wrong guess refuses a
paying miner on a path `tests/test_backward_compat.py` pins byte for byte. The
LingBot seasons are the ones where both sides really do call
`check_lingbot_layout`, which is what makes the promise keepable there. Closing
the gap means the backend adopting the package's π0.5 rules; until then
`openroboto check` is what those miners have, and `init --refresh` moves them
onto a live season that this gate does cover.

🔴 **It judges the repository listing, not the local directory.** The fee buys
a verdict on `hf_repo_id` at the commit that goes on chain, and that is not the
same set of files: `upload_folder` never deletes, and the repository id is
`{user}/pi05-{hotkey suffix}` -- one repository for the miner's whole career, so
round 7 is uploaded on top of rounds 1 to 6. A `.cache/` or a `*.tmp` left in
there by an earlier round is `LEFTOVER_UPLOAD_STATE` / `INCOMPLETE_FILE` to
admission and is **invisible** to anything that walks this round's output
directory. Checking the local copy and paying for the repository is the same
class of bug as the two sides using different rule books, one layer down.

`openroboto check` stays exactly where it was, and stays useful for the two
things this gate cannot do: it runs *before* the multi-gigabyte push, and it
reads `model.safetensors.index.json` off the disk, so it also evaluates the
shard and tensor rules that neither this gate nor admission can see.

## The competition is checked here, not inside the payment

Right before the fee is paid, and **every time** -- `openroboto check` is
optional and skipping it must not skip this. What it is for is one sentence: a
miner should never send TAO without being told which season it is going to and
how long that season has left. Their last `init` may have picked a competition
that has since ended while a new one opened, and on their terminal those two
situations look exactly the same.

It sits in this orchestration and not inside `execute_stake_burn` on purpose.
`openroboto burn` is a single-step command miners already script, its behaviour
is fixed (AGENTS.md §1), and a config from before competitions existed has
nothing to check anyway -- pushing the gate down there turns it into a pile of
"skip when…" branches instead of one gate on the path the documentation
teaches.

**There is no `--skip-precheck`, and `--force` does not skip it either.** An
escape hatch on this gate would be used, and afterwards we could not even show
that it had been.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from typing import Any

from openroboto_protocol.model_format import FormatIssueCode, FormatReport

from openroboto.commands.announce import perform_announce
from openroboto.commands.burn import perform_burn
from openroboto.commands.check import (
    check_files,
    resolve_layout,
    rules_label,
    tree_files,
    weights_subdir_of,
)
from openroboto.commands.upload import perform_upload
from openroboto.competition import BURN, PrecheckFailed, load_snapshot, precheck
from openroboto.config import Settings
from openroboto.console import fail, say
from openroboto.huggingface import TreeError, fetch_tree
from openroboto.round_state import (
    announced_commit,
    load_state,
    resolve_output_dir,
    resolve_round,
    save_state,
)


def add_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("submit", help="upload → burn → announce")
    parser.add_argument("--config", default="miner.yaml")
    parser.add_argument("--round", type=int, default=0)
    parser.add_argument("--output-dir", default="")
    parser.add_argument(
        "--force",
        action="store_true",
        help="ignore the completed state, burn again and re-announce",
    )
    parser.set_defaults(handler=run)


def run(args: argparse.Namespace) -> int:
    settings = Settings.load(args.config)
    round_num = resolve_round(args.round)
    output_dir = args.output_dir or resolve_output_dir(round_num)
    state = load_state(round_num)

    say(f"🦞 submit | round={round_num}")

    if state.get("step") == "announce" and not args.force:
        say(
            "⏭️  this round is already submitted. Pass --force to redo it "
            "(that burns TAO again)"
        )
        return 0

    if args.force:
        say("⚡ --force: ignoring the burn in the checkpoint -- this **burns again**")
        state.pop("burn_tx_hash", None)
        state.pop("burn_block", None)
        save_state(round_num, state)

    perform_upload(settings, round_num, output_dir, state, reuse_existing=True)

    if state.get("burn_tx_hash"):
        # The layout gate below is deliberately **not** run on this path. The
        # fee is already gone, and the only thing left that can make it count is
        # the announcement; refusing here would strand a paid submission with
        # nothing on chain -- turning a bad layout into a total loss instead of
        # a rejection. `--force` cleared this key above, so redoing a round does
        # go through the gate.
        say(
            f"⏭️  already burned: tx={str(state['burn_tx_hash'])[:16]}... "
            f"block={state.get('burn_block')}"
        )
    else:
        snapshot = load_snapshot(settings)
        if snapshot is None:
            # A config from before competitions existed. Nothing to check
            # against, and the old path is left byte for byte as it was.
            if not perform_burn(settings, round_num, state):
                return 1
        else:
            # Before the season check, not after: that one ends in a y/N prompt,
            # and asking someone to confirm a payment we are about to refuse
            # anyway teaches them to answer the prompt without reading it.
            if not layout_is_payable(settings, state):
                return 1
            try:
                verdict = precheck(settings, snapshot, datetime.now(UTC))
            except PrecheckFailed:
                return 1
            if verdict.kind != BURN:
                # `transfer` is a real competition setting that this client
                # cannot carry out yet. Falling through to the burn would pay
                # the right amount in the wrong way -- irreversibly, and the
                # submission would still not be paid for.
                fail(
                    f"{verdict.live.label} is paid for by {verdict.kind}, which "
                    f"this version cannot send yet. **Nothing was paid.**\n"
                    f"   → pip install -U openroboto"
                )
                return 1
            # The season id goes into the checkpoint, not straight into the
            # announcement, for the same reason `burn_tx_hash` does: the two
            # steps can be minutes and a crash apart, and a bare `openroboto
            # announce` afterwards has to put the *same* `cid` on chain that the
            # fee was just paid under. It is written before the payment so that
            # the pre-spend self-check sizes the payload this round will really
            # send, and it is the resolved id from the row the backend served a
            # moment ago -- never a number copied out of miner.yaml.
            state["competition_id"] = verdict.cid
            save_state(round_num, state)
            # The verdict travels with the payment rather than being re-derived
            # from `settings`: it is the proof that the season was confirmed **in
            # this run**, and it carries the fee that was confirmed with it.
            if not perform_burn(settings, round_num, state, verdict=verdict):
                return 1

    if not perform_announce(settings, round_num, state):
        return 1

    say("✅ submitted. Run `openroboto status` to see whether the backend accepted it")
    return 0


def layout_is_payable(settings: Settings, state: dict[str, Any]) -> bool:
    """Is this repository worth paying an entry fee for? False = do not pay.

    Three outcomes, and the two failing ones are **not** the same thing:

    - the rules judge the repository unfit → refuse. This is a verdict, and it
      is the cheap half of the asymmetry: the miner fixes the upload and runs
      the command again, having spent nothing;
    - the rules cannot be reached at all (HuggingFace is down, the listing does
      not come back) → **also refuse**, and say plainly that it is not a verdict
      on the model. See `_unlistable` for why that is the right way round;
    - otherwise → pay.

    Choosing the rule book can raise `ConfigError` (the installed protocol
    package is too old to hold this competition's rules). It is done *first*, so
    that refusal arrives without a network round trip in front of it, and it
    reaches `cli.main`, which prints it and exits 1 -- unpaid, which is the
    point.
    """
    layout = resolve_layout(settings)
    repo_id = str(state.get("hf_repo_id", ""))
    revision = announced_commit(state)
    where = f"{repo_id}@{revision[:8]}"

    try:
        entries = fetch_tree(repo_id, revision, settings.hf_token)
    except TreeError as exc:
        _unlistable(where, exc)
        return False

    files = tree_files(entries)
    # ponytail: no `weight_map` -- exactly what `judge_lingbot_tree` passes, so
    # this gate is admission's equal. The shard and tensor rules it leaves out
    # are not rejection reasons on either side (they surface at evaluation), and
    # `openroboto check` reads the index off the disk to cover them. If a real
    # miner ever loses a fee to a missing shard, fetch the index blob from this
    # same revision and pass it here.
    report = check_files(files, layout=layout)
    if report.errors or report.warnings:
        _unfit(where, report, layout, [file.path for file in files])
        return False

    say(f"✅ layout ok | {where} judged by the {rules_label(layout)} rules")
    return True


def _unlistable(where: str, exc: TreeError) -> None:
    """HuggingFace would not list the repository, so nothing was judged.

    🔴 This refuses to pay, and that is deliberate, so here is the argument in
    full because the opposite reading is defensible right up until you price it.

    AGENTS.md §4 says infrastructure trouble must not be reported as the user's
    fault. It is obeyed by what this *says* -- no `FormatIssueCode`, no claim
    about the model, the word "HuggingFace" in the first line. It is not obeyed
    by paying anyway: "we could not check, so we spent your money" is not
    generosity, it is today's behaviour with an apology attached, and the whole
    reason this gate exists is that today's behaviour lands on
    `HF_STRUCTURE_INVALID` after the fee, where nothing can be undone.

    Stopping consumes nothing. The upload is in the checkpoint and is reused, so
    `openroboto submit` picks up exactly here and re-uploads not one byte; the
    cost of being wrong is one command, against a non-refundable fee and a used
    queue slot for being wrong the other way.

    It is also the answer this CLI already gives one step later: an unreachable
    backend refuses the payment rather than guessing the season (AGENTS.md §1).
    Two gates on the same path answering "I do not know" differently is how a
    miner ends up unable to say what the tool will do with their money.
    """
    fail(
        f"Could not read the file list of {where}, so this submission's layout "
        f"was **not** judged -- and **nothing was paid, nothing was sent on "
        f"chain**.\n"
        f"   {exc}\n"
        f"   This is not a verdict on your model: the subnet judges the same "
        f"listing after the fee is paid, and a rejection there is final and not "
        f"refunded, so an answer we could not get is not one this will pay "
        f"through.\n"
        f"   → run `openroboto submit` again; your upload is kept and is not "
        f"pushed a second time"
    )


def _unfit(where: str, report: FormatReport, layout: Any, paths: list[str]) -> None:
    """The rules judged the repository, and the answer was no.

    Warnings stop the run as well as errors, which is stricter than admission
    and deliberately so: a `nested_too_deep` repository is *accepted* and then
    cannot be loaded, which costs the fee and the queue slot and returns no
    score -- the more expensive of the two outcomes, and the only one this
    command is in a position to prevent.
    """
    fail(
        f"{where} would not earn a score as it stands, so **nothing was paid "
        f"and nothing was sent on chain.**"
    )
    say(f"   rules: {rules_label(layout)}")
    for error in report.errors:
        say(f"   ❌ [{error.code.value}] {error.message}")
    for warning in report.warnings:
        say(f"   ⚠️  [{warning.code.value}] {warning.message}")
        if warning.code == FormatIssueCode.NESTED_TOO_DEEP:
            # Naming the directory is the whole value of this line: "your layout
            # is invalid" is not something anyone can act on, and the shape is
            # not the miner's invention -- the vendor's own post-trained
            # artifact ships under `checkpoints/global_step_N/hf_ckpt/`.
            say(
                f"      → your weights are in {weights_subdir_of(paths)}/ inside "
                f"the repository;"
            )
            say("        upload that directory as the repository root instead")
    if not report.errors:
        # Without this the natural reading of "the subnet accepts it" is "so
        # submit anyway", which is precisely the run that wastes the fee.
        say("   The subnet would accept this upload; the evaluator cannot load it.")
    say("   → fix the upload, then run `openroboto submit` again.")
    say("     `openroboto check <dir>` explains each line above against your own")
    say("     directory and names the one to upload instead. Nothing is refunded")
    say("     once the fee is paid, which is why this stops here.")
