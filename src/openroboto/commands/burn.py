"""`openroboto burn` -- burn TAO to pay the evaluation fee (the old
`rt.py burn`).

This is the only command that **spends money and cannot be undone**, so the
order is fixed: first refresh control.json to get this round's rate → then run
the pre-chain self-check → and only then send the transaction. If the
self-check does not pass, not a single cent is burned.
"""

from __future__ import annotations

import argparse
import logging
from typing import Any

from openroboto.chain import get_subtensor, open_wallet
from openroboto.competition import load_snapshot
from openroboto.config import Settings, refresh_burn_rate
from openroboto.console import fail, say
from openroboto.payment import execute_stake_burn
from openroboto.preflight import check_announce_ready, payload_size, payload_track
from openroboto.round_state import load_state, resolve_round, save_state

logger = logging.getLogger("openroboto")


def add_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("burn", help="Burn TAO to pay the evaluation fee")
    parser.add_argument("--config", default="miner.yaml")
    parser.add_argument("--round", type=int, default=0)
    parser.set_defaults(handler=run)


def run(args: argparse.Namespace) -> int:
    settings = Settings.load(args.config)
    round_num = resolve_round(args.round)
    state = load_state(round_num)

    if not perform_burn(settings, round_num, state):
        return 1

    say(
        f"✅ burn done | tx={state['burn_tx_hash'][:16]}... block={state['burn_block']}"
    )
    say(
        "   → Next, run `openroboto announce` (**required** -- without it "
        "nobody can see this burn)"
    )
    return 0


def perform_burn(settings: Settings, round_num: int, state: dict[str, Any]) -> bool:
    """Burn once and write the tx and block into the checkpoint. Returning
    False means the self-check did not pass and nothing was spent."""
    settings.require_for_chain()
    if load_snapshot(settings) is None:
        refresh_burn_rate(settings, logger)
    elif settings.burn_rate_tao is None:
        # With a competition section, the rate comes from that season's own
        # `params.fee`, established by the pre-payment check in `submit`.
        # control.json's rate is subnet-wide and is not a substitute: two
        # sources for one number, with no rule for which wins when they differ.
        # Reaching here means nothing checked it, so nothing is burned.
        fail(
            "This workspace mines a specific competition, and that competition's"
            " fee is confirmed against the backend in the moment before it is"
            " paid -- which has not happened, so **nothing was burned**.\n"
            "   → run `openroboto submit`; it uploads, confirms the competition"
            " and pays in one go\n"
            "   → `openroboto burn` on its own is for configs from before there"
            " was more than one competition"
        )
        return False

    # If the rate could not be parsed, **stop right here**; do not guess. The
    # old code defaulted to 0.01 while production was 0.1: when the
    # control.json fetch failed, the miner burned ten times too little, the
    # backend checked the amount and rejected outright, and the TAO was not
    # refunded. This is the last fail-closed gate before spending money -- do
    # not give it a fallback value.
    if settings.burn_rate_tao is None:
        # This message **does not name a concrete amount**. The rate is
        # published by the subnet and changes; hardcoding a number is just
        # growing the 0.01 we deleted back somewhere else.
        fail(
            f"Could not get the evaluation fee rate for round {round_num};"
            f" **not** burning.\n"
            f"   The only authoritative source for the rate is"
            f" `payment.burn_rate_tao` in the control.json published by the"
            f" subnet; burn the wrong amount and the backend rejects it, and"
            f" the TAO is not refunded -- so this will not guess a value for"
            f" you.\n"
            f"   → Run `openroboto doctor` first to check whether control.json"
            f" is reachable.\n"
            f"   → To set it by hand, copy the current value from control.json"
            f" into `payment.burn_rate_tao` in miner.yaml (copying it wrong"
            f" means burning for nothing -- double-check it)"
        )
        return False

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

    say(
        f"🔥 About to burn {settings.burn_rate_tao} TAO "
        f"(netuid={settings.netuid}, irreversible)"
    )

    subtensor = get_subtensor(settings.network)
    try:
        wallet = open_wallet(settings)
        receipt = execute_stake_burn(
            subtensor=subtensor,
            wallet=wallet,
            netuid=settings.netuid,
            amount_tao=settings.burn_rate_tao,
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
