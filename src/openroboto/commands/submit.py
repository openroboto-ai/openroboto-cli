"""`openroboto submit` -- upload → resolve the competition → check the layout →
check that this model is not already entered → pay → announce (the old
`rt.py submit`).

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

⚠️ A config from before competitions existed does not reach it either, but for a
blunter reason than it used to have: **it does not reach the payment at all**.
`_no_season` refuses the run before the upload. There is no longer a path through
this command that spends money without a season attached to it.

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

It is fetched in two halves. `resolve_competition` gets the row and judges it,
`confirm_payment` shows the result and asks -- and the gates below run between
them, because they need the live row (which rule book, which season's entry
list) and because the prompt must be the last thing that happens before the
money moves. A miner asked to confirm a payment that the next gate refuses
learns to answer prompts without reading them.

It sits in this orchestration and not inside `execute_stake_burn` on purpose: the
check ends in a y/N prompt and prints who is being paid, which is a conversation
with the miner, not something to run from inside the extrinsic layer. What
`perform_burn` does hold is the *consequence* -- it will not spend without the
`Verdict` this produced -- so the gate cannot be walked around by calling the
lower layer directly. `openroboto burn` on its own refuses for the same reason
(`commands/burn.py`): it could ask the backend, but it could not run the layout
gate above, and a command that pays after only half the checks is this hole with
a different name on it.

**There is no `--skip-precheck`, and `--force` does not skip it either.** An
escape hatch on this gate would be used, and afterwards we could not even show
that it had been.

## What `--force` really is, and what closes its one hole

It means "this round is finished, do it again anyway", and what it clears is the
payment: the upload stays. So the second run re-pays for **the same commit** --
which the subnet counts once. Admission finds the row already there, files the
new one as `skipped`, and the fee is spent on nothing. Nothing about the flag
made that visible: the checkpoint still held `hf_commit`, so the upload was
skipped, the payment went through and the miner saw a normal successful run.

The fix is not in the flag. `slot_is_free` asks the backend, before every
payment, whether that commit already occupies its dedup slot, so this closes
whether the second run came from `--force`, from a re-run after a crash, or from
a miner submitting the same checkpoint twice by hand. The flag's help text says
what it does instead of implying a fresh submission.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from typing import Any

from openroboto_protocol.model_format import FormatIssueCode, FormatReport
from openroboto_protocol.model_hash import model_hash_from_hf_tree
from openroboto_protocol.schemas import Competition

from openroboto.backend_api import BackendError, fetch_roster
from openroboto.commands.announce import perform_announce
from openroboto.commands.burn import perform_burn, perform_transfer
from openroboto.commands.check import (
    check_files,
    layout_of,
    rules_label,
    tree_files,
    weights_subdir_of,
)
from openroboto.commands.upload import perform_upload
from openroboto.competition import (
    BURN,
    PrecheckFailed,
    Verdict,
    confirm_payment,
    load_snapshot,
    resolve_competition,
)
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

#: One request instead of a paging loop, the same figure `status` uses -- it is
#: the backend's maximum page size, and it is asked for **one hotkey's** rows in
#: one season, so a second page would mean a miner who has paid a thousand entry
#: fees for one competition.
ROSTER_LIMIT = 1000


def add_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "submit", help="upload → pay the entry fee → announce"
    )
    parser.add_argument("--config", default="miner.yaml")
    parser.add_argument("--round", type=int, default=0)
    parser.add_argument("--output-dir", default="")
    parser.add_argument(
        "--force",
        action="store_true",
        help="re-announce a round that is already done, paying the entry fee a "
        "second time. The upload is reused as it is, so this re-submits the same "
        "model -- which the subnet counts once, however many times it is paid for",
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
            "(that pays the entry fee again)"
        )
        return 0

    if args.force:
        # "the payment", not "the burn": on the real track the same two keys hold
        # a transfer, and a miner told they are about to burn 2 TAO looks for
        # stake that will never exist.
        say(
            "⚡ --force: ignoring the payment in the checkpoint -- "
            "this **pays the entry fee again**"
        )
        state.pop("burn_tx_hash", None)
        state.pop("burn_block", None)
        save_state(round_num, state)

    snapshot = load_snapshot(settings)
    if snapshot is None and not state.get("burn_tx_hash"):
        # Before the upload, not after it: this run cannot end in a payment, and
        # finding that out on the far side of several gigabytes is a cost with
        # nothing to show for it. A round that has **already** burned is exempt
        # and falls through to the announcement below -- see the comment there;
        # refusing a paid submission its commitment is the one outcome worse
        # than a wasted push.
        _no_season(args.config)
        return 1

    perform_upload(settings, round_num, output_dir, state, reuse_existing=True)

    if state.get("burn_tx_hash"):
        # The layout gate below is deliberately **not** run on this path. The
        # fee is already gone, and the only thing left that can make it count is
        # the announcement; refusing here would strand a paid submission with
        # nothing on chain -- turning a bad layout into a total loss instead of
        # a rejection. `--force` cleared this key above, so redoing a round does
        # go through the gate.
        say(
            f"⏭️  already paid: tx={str(state['burn_tx_hash'])[:16]}... "
            f"block={state.get('burn_block')}"
        )
    elif snapshot is not None:
        # `snapshot is None` is already impossible here -- the guard above
        # refused every unpaid run without one. It is spelled out rather than
        # asserted so that if the guard is ever moved, this falls through to the
        # announcement (which refuses an unpaid round) instead of paying.
        #
        # The order of these three is the whole point of the season check being
        # in two halves. The live row comes first because both gates below need
        # it -- the layout gate to know which rule book judges this repository,
        # the dedup gate to know which season's entry list to ask. The prompt
        # comes last because asking someone to confirm a payment that the next
        # gate is about to refuse teaches them to answer prompts without
        # reading them.
        try:
            verdict = resolve_competition(settings, snapshot, datetime.now(UTC))
            if not layout_is_payable(settings, state, verdict.live):
                return 1
            if not slot_is_free(settings, state, verdict):
                return 1
            confirm_payment(verdict)
        except PrecheckFailed:
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
        # this run**, and it carries the fee that was confirmed with it --
        # including *how* it is collected.
        #
        # 🔴 The branch is on `verdict.kind`, i.e. on `params.fee.kind` as the
        # backend served it seconds ago, and not on the track or the adapter.
        # Those are two more names for the same fact and they would disagree on
        # the first season that breaks the pattern -- while paying. `fee_of()`
        # has already refused any third word, so there is no default case to
        # fall through into (falling through to the burn would pay the right
        # amount in a way that reaches nobody, irreversibly, and leave the
        # submission unpaid).
        pay = perform_burn if verdict.kind == BURN else perform_transfer
        if not pay(settings, round_num, state, verdict=verdict):
            return 1

    if not perform_announce(settings, round_num, state):
        return 1

    say("✅ submitted. Run `openroboto status` to see whether the backend accepted it")
    return 0


def _no_season(config_path: str) -> None:
    """This workspace does not say which competition it mines, so it cannot pay.

    The fee, the collection address and the season the submission is filed under
    all come from the `competition:` section, and there is no longer a
    subnet-wide rate standing behind it: `control.json`'s `payment` block served
    one number to a subnet that runs several seasons at once, and paying it
    bought a place in whichever season the backend defaults to (`commands/burn.py`
    has the full account). Refusing is the outcome that costs nothing.

    ⚠️ The miner reading this has only this message to go on, so it names the
    command that repairs the file rather than describing the defect.
    """
    fail(
        f"{config_path} does not say which competition this workspace mines "
        f"(no `competition:` section), so there is no entry fee to pay and no "
        f"season to submit to. **Nothing was uploaded, paid or sent on chain.**\n"
        f"   → `openroboto init --refresh` writes that section from the backend "
        f"and leaves the rest of {config_path} byte for byte as it is (the "
        f"previous version is kept as {config_path}.bak)\n"
        f"   → or `openroboto init <directory>` for a fresh workspace, then copy "
        f"your wallet and HuggingFace settings across\n"
        f"   Configs written before the subnet ran more than one competition are "
        f"no longer supported: a fee paid with no season attached is filed under "
        f"whichever season the backend defaults to, and it is not refunded."
    )


def layout_is_payable(
    settings: Settings, state: dict[str, Any], live: Competition
) -> bool:
    """Is this repository worth paying an entry fee for? False = do not pay.

    Three outcomes, and the two failing ones are **not** the same thing:

    - the rules judge the repository unfit → refuse. This is a verdict, and it
      is the cheap half of the asymmetry: the miner fixes the upload and runs
      the command again, having spent nothing;
    - the rules cannot be reached at all (HuggingFace is down, the listing does
      not come back) → **also refuse**, and say plainly that it is not a verdict
      on the model. See `_unlistable` for why that is the right way round;
    - otherwise → pay.

    🔴 **The rule book comes from `live`, not from `miner.yaml`.** They are the
    same row copied at two different times, and the gap between them is where
    this gate stops being one: a season that changed its `base_model_family`
    after `init` is judged here by the rules the miner signed up for and by
    admission by the rules it has now -- passing here, `HF_STRUCTURE_INVALID`
    there, after the fee. The whole promise of this gate is that it answers with
    the same book admission will use, and only the live row can say which that
    is.

    Choosing the rule book can raise `ConfigError` (the installed protocol
    package is too old to hold this competition's rules). It is done *first*, so
    that refusal arrives without a network round trip in front of it, and it
    reaches `cli.main`, which prints it and exits 1 -- unpaid, which is the
    point.
    """
    layout = layout_of(live.adapter, live.base_model_family or "", live.params)
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

    # Last, not first: this is the question the layout rules cannot ask, and
    # asking it earlier would answer a bare LoRA adapter with "no LFS objects"
    # instead of naming what is actually wrong with it.
    if not model_hash_from_hf_tree(entries):
        _no_weights(where)
        return False

    say(f"✅ layout ok | {where} judged by the {rules_label(layout)} rules")
    return True


def _no_weights(where: str) -> None:
    """The repository holds no LFS object at all, so it holds no weights.

    🔴 **The fingerprint is what the subnet identifies a model by**, and an
    empty one is a sentinel: admission's sixth gate files it as
    `MODEL_HASH_FAILED` -- terminal, after the fee. The repository that produces
    it is one where the pointer files were pushed and the weights were not, or
    where the directory that was uploaded held no model at all.

    The real track already refuses this one step earlier, in `upload`, because
    it has to compute the fingerprint anyway to put it on chain. **The
    simulation track never computes it at all** -- those repositories are public,
    so the backend does it itself and the `m` key is not sent -- which left the
    whole simulation side finding out about an empty fingerprint only from
    admission, having paid. The listing is already in hand here, so the same
    question is asked for both tracks in one place.

    ⚠️ **A gap this cannot close, on purpose.** The backend narrows the listing
    to the weight files a season's rule book recognizes *before* fingerprinting
    (`app/domain/hf_layout.py::select_lingbot_weight_entries`), so a repository
    that has LFS objects but none of them among the six shards still reaches
    `MODEL_HASH_FAILED` while passing here. The direction is the safe one --
    this can wave through what the backend refuses, it cannot refuse what the
    backend would take -- and closing it means moving that selector into the
    protocol package, which is a separate piece of work and not one to do on
    the way past.
    """
    fail(
        f"{where} contains no LFS file at all, so it holds no model weights and "
        f"the subnet could not fingerprint it. **Nothing was paid, nothing was "
        f"sent on chain.**\n"
        f"   Usually this means only the pointer files reached HuggingFace, or "
        f"the directory that was uploaded held no model.\n"
        f"   The fee buys an evaluation of the weights at this commit; a "
        f"submission the subnet cannot fingerprint is rejected after the fee is "
        f"paid, and that rejection is final.\n"
        f"   → check the upload on huggingface.co, then run `openroboto submit` "
        f"again"
    )


def slot_is_free(settings: Settings, state: dict[str, Any], verdict: Verdict) -> bool:
    """Would this commit be a new submission, or one the backend already has?
    False = do not pay.

    🔴 **The subnet counts a model once.** The dedup key is
    `(hotkey, competition_id, hf_commit)` -- one model one entry, *not* one
    entry per season, so entering the same season again with a different model
    is normal and pays again. What is not normal is paying a second time for the
    same commit: admission finds the row already there and files the new one as
    `skipped`, building nothing. The fee is spent, nothing is queued, nothing is
    refunded.

    🔴 **The answer comes from the backend as a conclusion, not as ingredients.**
    A row that was pushed aside by a later submission (`superseded:`) still holds
    the slot, and on the `status` column it is indistinguishable from a real
    rejection -- so a copy of the rule written here would be wrong in the
    direction that pays: "it says rejected, the slot must be free". Hence
    `counts_as_submitted` on the roster row, computed by the one function that
    owns that rule (`submission_writes.counts_as_submitted`).

    A rejection for a real reason does **not** hold the slot, and that is the
    design: a miner who fixes what was wrong may submit the same model again.

    Not being able to ask is a refusal, like everything else on this path: a
    backend that cannot answer "have I already paid for this" is not one to pay
    on the assumption that the answer is no.
    """
    hotkey = str(state.get("hotkey_ss58", ""))
    commit = announced_commit(state)
    try:
        roster = fetch_roster(
            settings.backend_url, verdict.cid, hotkey=hotkey, limit=ROSTER_LIMIT
        )
    except BackendError as exc:
        fail(
            f"Could not ask the backend whether this commit has already been "
            f"submitted to {verdict.live.label}, so **nothing was paid and "
            f"nothing was sent on chain**.\n"
            f"   {exc}\n"
            f"   Paying twice for one commit buys nothing: the second submission "
            f"is skipped, and the fee is not refunded.\n"
            f"   → run `openroboto submit` again; your upload is kept and is not "
            f"pushed a second time"
        )
        return False

    if not any(
        row.hf_commit == commit and row.counts_as_submitted for row in roster.data
    ):
        return True

    fail(
        f"This model is already entered in {verdict.live.label} "
        f"({verdict.live.track}/{verdict.live.seq}), so paying again would buy "
        f"nothing. **Nothing was paid, nothing was sent on chain.**\n"
        f"   commit {commit[:12]}... under hotkey {hotkey[:12]}...\n"
        f"   The subnet evaluates one model version once: a second submission of "
        f"the same commit is skipped on arrival, and the entry fee for it is not "
        f"refunded.\n"
        f"   → train again and upload the new checkpoint, then `openroboto "
        f"submit` -- a different model in the same season is a normal, and paid "
        f"for, second entry\n"
        f"   → `openroboto status` shows what the subnet did with the entry you "
        f"already have"
    )
    return False


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
