"""`openroboto submit` -- upload → check the competition → pay → announce (the
old `rt.py submit`).

The steps reuse the very same implementations from the `upload` / `burn` /
`announce` modules. The old `rt.py` **copied all three again** inside
`cmd_submit`, and the self-checks and skip conditions in the two places
gradually grew apart; here there is only one copy.

The checkpoint makes this command naturally re-entrant: what has been uploaded
is not uploaded again, what has been paid for is not paid for again.

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

from openroboto.commands.announce import perform_announce
from openroboto.commands.burn import perform_burn
from openroboto.commands.upload import perform_upload
from openroboto.competition import BURN, PrecheckFailed, load_snapshot, precheck
from openroboto.config import Settings
from openroboto.console import fail, say
from openroboto.round_state import (
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
            # The fee comes from the row that was just checked, and from
            # nowhere else -- see `competition` for why control.json's
            # subnet-wide rate is not a substitute.
            settings.burn_rate_tao = verdict.amount_tao
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
            if not perform_burn(settings, round_num, state):
                return 1

    if not perform_announce(settings, round_num, state):
        return 1

    say("✅ submitted. Run `openroboto status` to see whether the backend accepted it")
    return 0
