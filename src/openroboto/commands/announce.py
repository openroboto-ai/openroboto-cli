"""`openroboto announce` -- write the submission on chain (the old
`rt.py announce`).

**This step must be completed after a burn**: without a commitment, that burn
does not exist as far as the backend is concerned. The payload bytes are
produced by `openroboto-protocol`; this repo no longer assembles a JSON of its
own.
"""

from __future__ import annotations

import argparse
from typing import Any

from openroboto_protocol.commitment import CommitmentFieldError, check_payload

from openroboto.chain import (
    build_payload,
    get_subtensor,
    open_wallet,
    submit_announcement,
)
from openroboto.config import ConfigError, Settings
from openroboto.console import fail, say
from openroboto.huggingface import commit_sha_from_url
from openroboto.preflight import check_burn_window, payload_track
from openroboto.round_state import (
    competition_id,
    load_state,
    model_hash,
    resolve_round,
    save_state,
)


def add_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "announce", help="Commit the submission announcement on chain"
    )
    parser.add_argument("--config", default="miner.yaml")
    parser.add_argument("--round", type=int, default=0)
    parser.set_defaults(handler=run)


def run(args: argparse.Namespace) -> int:
    settings = Settings.load(args.config)
    round_num = resolve_round(args.round)
    state = load_state(round_num)

    if not perform_announce(settings, round_num, state):
        return 1
    say("   → Run `openroboto status` to see whether the backend took the submission")
    return 0


def perform_announce(settings: Settings, round_num: int, state: dict[str, Any]) -> bool:
    """Send the commitment and, on success, mark the step as announce."""
    settings.require_for_chain()

    hf_repo_id = str(state.get("hf_repo_id", ""))
    hf_url = str(state.get("hf_url", ""))
    if not hf_repo_id or not hf_url:
        raise ConfigError(
            "No HF repo info in the checkpoint -- run `openroboto upload` first"
        )

    # Take the commit SHA from the URL (what the upload returns is this very
    # commit); when the URL carries no commit segment, fall back to the one
    # stored in the checkpoint from `repo_info().sha`. The old code only knew
    # about the URL and wrote `c` as an empty string when it could not get one
    # -- without a commit the backend cannot verify the model, which amounts to
    # burning TAO and handing in a worthless submission.
    hf_commit = commit_sha_from_url(hf_url) or str(state.get("hf_commit", ""))

    subtensor = get_subtensor(settings.network)
    try:
        wallet = open_wallet(settings)
        current_block = subtensor.get_current_block()
        block_hash = subtensor.get_block_hash(current_block)
        # The window check comes first, and nothing is printed before it.
        #
        # These two lines used to be the other way round, so a refused announce
        # ended with "📡 committing on chain" as the last thing on screen while
        # no extrinsic was ever sent. The exit code was already 1, which is what
        # a script reads -- but a person reads the last line, and that line said
        # the opposite of what happened. Spent ten minutes checking the chain,
        # then the database, then the ingest logs, for a commitment that had
        # been deliberately not sent.
        burn_block = int(state.get("burn_block", 0) or 0)
        if not _burn_window_ok(settings, burn_block, current_block):
            say("   → nothing was sent on chain, and no transaction fee was paid")
            return False

        payload = build_payload(
            hotkey_ss58=str(state.get("hotkey_ss58", "")) or wallet.hotkey.ss58_address,
            block_hash=str(block_hash),
            hf_commit=hf_commit,
            round_num=round_num,
            hf_repo_id=hf_repo_id,
            # `b` / `bb` are the **payment credential** -- which transaction,
            # which block. Whether that payment was a burn or a transfer to the
            # season's coldkey is decided by the season `cid` points at; there
            # is no second pair of keys for it.
            burn_tx_hash=str(state.get("burn_tx_hash", "")),
            burn_block=burn_block,
            # Both come from the checkpoint, and both are `None` when absent --
            # which is what a config from before competitions existed produces,
            # and what makes its bytes identical to what it wrote a year ago.
            competition_id=competition_id(state),
            model_hash=model_hash(state),
        )
        # The last gate, and like the window check above it runs **before
        # anything is printed**: a refused announce that ends with "committing
        # on chain" on screen sends the miner looking through the chain, the
        # database and the ingest logs for an extrinsic that was deliberately
        # never sent (fixed once already, 2026-08-19 -- do not reorder these).
        #
        # It cannot save the fee, which is already gone by now; what it saves is
        # the extrinsic fee and the belief that the submission is in. The gate
        # that runs before the money is `preflight.check_announce_ready`.
        try:
            check_payload(payload, payload_track(settings))
        except CommitmentFieldError as exc:
            fail(
                f"This commitment would be refused by the backend over its "
                f"`{exc.field}` field, so it is **not** being sent.\n"
                f"   {exc}\n"
                f"   Your payment stays valid -- fix this and run `openroboto "
                f"announce` again. **Do not pay a second time.**"
            )
            say("   → nothing was sent on chain, and no transaction fee was paid")
            return False

        say(
            f"📡 committing on chain | round={round_num} "
            f"repo={hf_repo_id} block={current_block}"
        )
        result = submit_announcement(subtensor, wallet, settings.netuid, payload)
    finally:
        subtensor.close()

    if not result.ok:
        fail(
            "The commitment was not confirmed on chain. The burn already"
            " happened -- **do not burn again**.\n"
            "   This may be only a wait timeout while the transaction did make"
            " it into a block, so check once first: `openroboto status`.\n"
            "   Once you have confirmed the backend did not receive it, run"
            " `openroboto announce` again (the burn_tx in the checkpoint is"
            " reused)"
        )
        return False

    if result.confirmed:
        say(
            f"✅ commitment on chain | ref={result.extrinsic_ref} "
            f"fee={result.fee_tao:.6f} TAO"
        )
    else:
        # The SDK reported success but gave back no block number
        # (`payment/burn.py:98` records this SDK behavior). Do not treat it as
        # a failure -- the transaction really was sent; but do not claim it is
        # "on chain" either.
        say(
            f"✅ commitment submitted | fee={result.fee_tao:.6f} TAO\n"
            f"   ⚠️  The SDK returned no block number -- use `openroboto status`"
            f" to verify it made it into a block"
        )
    state["step"] = "announce"
    state["status"] = "completed"
    save_state(round_num, state)
    return True


def _burn_window_ok(settings: Settings, burn_block: int, current_block: int) -> bool:
    """Run the window check and tell the miner the conclusion. The decision
    itself lives in `preflight` (a pure function)."""
    blocked, warning = check_burn_window(
        burn_block, current_block, settings.burn_block_window
    )
    if blocked:
        fail(blocked)
        return False
    if warning:
        say(f"⚠️  {warning}")
    return True
