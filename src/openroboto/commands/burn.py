"""`openroboto burn` -- burn TAO to pay a competition's entry fee (the old
`rt.py burn`).

This is the only command that **spends money and cannot be undone**, so what it
refuses to do matters more than what it does. The amount has exactly one source:
the `Verdict` that `competition.precheck()` returns, which carries both the
figure and the season it was quoted for, and which exists only if the backend was
asked in this run. `perform_burn` does not run without one.

## Two sources this deliberately no longer has

🔴 **`control.json`.** Its `payment.burn_rate_tao` is one number for the whole
subnet, and the subnet runs several seasons at once -- `sim/1` charges 0.1 TAO
while `real/1` charges 2. A subnet-wide rate is therefore not an answer to "what
does this submission cost"; it is right for whichever season happens to match and
silently wrong for the rest. It stayed reachable here for workspaces with no
`competition:` section, and `openroboto init` has not produced such a workspace
since seasons existed, so that branch served only installs from before the
rebuild -- which are not supported (ADR 05).

🔴 **`payment.burn_rate_tao` in `miner.yaml`.** An amount typed by hand satisfies
"there is a number" while answering nothing about *which competition* is being
paid for. That gap was payable: a hand-filled rate skipped the season check, so
no `competition_id` reached the checkpoint, so `announce` sent a payload with no
`cid`, so the backend filed the submission under the archived π0.5 season -- the
fee spent, the commitment on chain, the backend acknowledging it, all of it
landing on the wrong competition without one error printed anywhere.

## Why `openroboto burn` on its own refuses

A verdict can only be had by asking the backend, and asking it is one of the two
gates `openroboto submit` runs before it pays; the other judges the uploaded
repository's layout, which is what stops a fee from buying a rejection. A
standalone `burn` that fetched its own verdict would skip that second gate and
reopen it under a different command name. So this command stops and names the one
that runs both.
"""

from __future__ import annotations

import argparse
from typing import Any

from openroboto.chain import get_subtensor, open_wallet
from openroboto.competition import Verdict
from openroboto.config import Settings
from openroboto.console import fail, say
from openroboto.payment import execute_stake_burn
from openroboto.preflight import check_announce_ready, payload_size, payload_track
from openroboto.round_state import save_state


def add_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("burn", help="Burn TAO to pay the evaluation fee")
    parser.add_argument("--config", default="miner.yaml")
    parser.add_argument("--round", type=int, default=0)
    parser.set_defaults(handler=run)


def run(args: argparse.Namespace) -> int:
    """Refuse, and name the command that can pay.

    The subcommand is kept rather than removed: miners have it in scripts and in
    tutorials, and a sentence about where the fee now comes from is worth more to
    them than argparse's "invalid choice". See the module docstring for why it
    cannot pay on its own.
    """
    fail(
        "`openroboto burn` cannot pay an entry fee on its own, so **nothing was"
        " burned**.\n"
        "   The amount and the competition it pays for both come from the"
        " backend, asked in the moment before the money moves, and this command"
        " has nowhere to ask from.\n"
        "   → run `openroboto submit`: it uploads, judges the layout, confirms"
        " the competition and pays, in that order\n"
        "   Setting `payment.burn_rate_tao` in miner.yaml is not a way around"
        " this: it supplies an amount, not a season. Paid that way the submission"
        " carries no competition id and the backend files it under whichever"
        " season it defaults to -- with the TAO already gone."
    )
    return 1


def perform_burn(
    settings: Settings,
    round_num: int,
    state: dict[str, Any],
    verdict: Verdict,
) -> bool:
    """Burn once and write the tx and block into the checkpoint. Returning False
    means a self-check did not pass and nothing was spent.

    `verdict` is this run's season check (`competition.precheck`) and it is
    required, not optional: it is the proof that the backend was asked which
    competition this fee is for, and it carries the amount that was confirmed
    together with that answer. See the module docstring for what the fee bought
    while it was optional.
    """
    settings.require_for_chain()

    # The amount stays a local. Writing it back onto `settings` would put a
    # season's figure into the field that holds the subnet-wide rate, and the
    # next reader could no longer tell which of the two they were looking at.
    amount_tao = verdict.amount_tao

    # The track decides which fields the payload must carry, and this is the
    # last look at them before the money moves.
    reasons = check_announce_ready(state, round_num, payload_track(settings))
    if reasons:
        fail(f"Pre-chain self-check failed (round {round_num}); **not** burning:")
        for reason in reasons:
            say(f"   • {reason}")
        return False
    say(
        f"✅ self-check passed | commitment payload "
        f"{payload_size(state, round_num)}/512 bytes"
    )

    say(f"🔥 About to burn {amount_tao} TAO (netuid={settings.netuid}, irreversible)")

    subtensor = get_subtensor(settings.network)
    try:
        wallet = open_wallet(settings)
        receipt = execute_stake_burn(
            subtensor=subtensor,
            wallet=wallet,
            netuid=settings.netuid,
            amount_tao=amount_tao,
            limit_price_rao=settings.limit_price_rao,
        )
    finally:
        subtensor.close()

    state["burn_tx_hash"] = receipt.tx_hash
    state["burn_block"] = receipt.block_number
    state["step"] = "burn"
    state["status"] = "completed"
    save_state(round_num, state)
    return True
