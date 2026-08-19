"""`openroboto submit` -- upload → burn → announce, end to end (the old
`rt.py submit`).

The three steps reuse the very same implementations from the `upload` /
`burn` / `announce` modules. The old `rt.py` **copied all three again** inside
`cmd_submit`, and the self-checks and skip conditions in the two places
gradually grew apart; here there is only one copy.

The checkpoint makes this command naturally re-entrant: what has been uploaded
is not uploaded again, what has been burned is not burned again.
"""

from __future__ import annotations

import argparse

from openroboto.commands.announce import perform_announce
from openroboto.commands.burn import perform_burn
from openroboto.commands.upload import perform_upload
from openroboto.config import Settings
from openroboto.console import say
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
    elif not perform_burn(settings, round_num, state):
        return 1

    if not perform_announce(settings, round_num, state):
        return 1

    say("✅ submitted. Run `openroboto status` to see whether the backend accepted it")
    return 0
