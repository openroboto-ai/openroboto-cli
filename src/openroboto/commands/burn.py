"""`openroboto burn` -- burn TAO to pay a competition's entry fee.

This is the only command that **spends money and cannot be undone**, so what it
refuses to do matters more than what it does. The amount has exactly one source:
the `Verdict` that `competition.resolve_competition()` returns, which carries
both the figure and the season it was quoted for, and which exists only if the
backend was asked in this run. `perform_burn` does not run without one.

🔴 **An amount on its own is not a way to pay.** A number says how much, never
which competition, so a fee paid from one is filed under whichever season the
backend defaults to -- spent, on chain, acknowledged, and against the wrong
competition without one error printed anywhere. That is why the verdict is a
required argument rather than an optional override: the signature is the gate.

## Why `openroboto burn` on its own refuses

A verdict can only be had by asking the backend, and asking it is one of the two
gates `openroboto submit` runs before it pays; the other judges the uploaded
repository's layout, which is what stops a fee from buying a rejection. A
standalone `burn` that fetched its own verdict would skip that second gate and
reopen it under a different command name. So this command stops and names the one
that runs both.
"""

from __future__ import annotations

from typing import Any

from openroboto.chain import get_subtensor, open_wallet
from openroboto.commands.announce import _competition_seq
from openroboto.competition import Verdict
from openroboto.competition_state import save_state
from openroboto.config import Settings
from openroboto.console import fail, say
from openroboto.payment import BurnReceipt, execute_stake_burn, execute_transfer
from openroboto.preflight import check_announce_ready, payload_size, payload_track


def perform_burn(
    settings: Settings,
    competition_id: int,
    state: dict[str, Any],
    verdict: Verdict,
) -> bool:
    """Burn once and write the tx and block into the checkpoint. Returning False
    means a self-check did not pass and nothing was spent.

    `verdict` is this run's season check (`competition.resolve_competition`) and it is
    required, not optional: it is the proof that the backend was asked which
    competition this fee is for, and it carries the amount that was confirmed
    together with that answer. See the module docstring for what the fee bought
    while it was optional.
    """
    if not _ready_to_spend(settings, competition_id, state, doing="burning"):
        return False

    # The amount stays a local. Writing it back onto `settings` would put a
    # season's figure into the field that holds the subnet-wide rate, and the
    # next reader could no longer tell which of the two they were looking at.
    amount_tao = verdict.amount_tao
    say(f"🔥 About to burn {amount_tao} TAO (netuid={settings.netuid}, irreversible)")

    subtensor = get_subtensor(settings.network)
    try:
        wallet = open_wallet(settings)
        if not pays_as_the_hotkeys_owner(subtensor, wallet, state):
            return False
        receipt = execute_stake_burn(
            subtensor=subtensor,
            wallet=wallet,
            netuid=settings.netuid,
            amount_tao=amount_tao,
        )
    finally:
        subtensor.close()

    _record(competition_id, state, receipt)
    return True


def perform_transfer(
    settings: Settings,
    competition_id: int,
    state: dict[str, Any],
    verdict: Verdict,
) -> bool:
    """Pay a **transfer** season's fee: ordinary TAO into `params.fee.coldkey`.

    The real track's counterpart to `perform_burn`, and deliberately shaped the
    same way -- same self-check before the money moves, same two fields written
    into the checkpoint afterwards (`b` / `bb` are shared by both tracks, spec 10
    §3.5). Everything that differs is the extrinsic itself.

    🔴 **The address comes from the verdict, never from `miner.yaml`.**
    `judge()` has already refused a season whose `coldkey` is null and refused
    one whose address moved since `init`; taking it from the snapshot here would
    put those two checks back to being advisory, on the one value where being
    wrong means the fee is irrecoverably in a stranger's account.

    `coldkey` cannot be `None` at this point for the same reason -- `judge()`
    checks existence before equality and raises. Asserting it rather than
    defaulting keeps that guarantee where it is enforced instead of quietly
    growing a second, weaker copy of it here.
    """
    if not _ready_to_spend(settings, competition_id, state, doing="paying"):
        return False

    amount_tao = verdict.amount_tao
    dest = verdict.fee.coldkey
    assert dest is not None  # judge() refuses a season with a null address

    say(
        f"💸 About to transfer {amount_tao} TAO to {dest} "
        f"(network={settings.network}, irreversible)"
    )

    subtensor = get_subtensor(settings.network)
    try:
        wallet = open_wallet(settings)
        if not pays_as_the_hotkeys_owner(subtensor, wallet, state):
            return False
        receipt = execute_transfer(
            subtensor=subtensor,
            wallet=wallet,
            dest_coldkey=dest,
            amount_tao=amount_tao,
        )
    finally:
        subtensor.close()

    _record(competition_id, state, receipt)
    return True


def pays_as_the_hotkeys_owner(
    subtensor: Any, wallet: Any, state: dict[str, Any]
) -> bool:
    """Does the chain agree that this wallet may pay for this hotkey? False =
    do not pay.

    🔴 **The most expensive check in this file, and the last one that is free.**
    The backend does not take the miner's word for who paid: it reads the
    extrinsic's signer off the chain and compares it against the chain's own
    `hotkey → owner coldkey` mapping for the hotkey in the commitment
    (`verification/transfer.py` step 6, `verification/burn.py` check 1). A
    mismatch is `fee_payer_not_owner`, which is `rejected` -- final, no retry,
    and the fee has already left the wallet. On the real track that is 2 TAO
    into the season's collection address, which nothing brings back.

    The comparison is made here from exactly the two values that comparison
    reads, and the same way round:

    - the hotkey is the one the commitment will carry (`state["hotkey_ss58"]`,
      which `_ready_to_spend` has already refused to proceed without), **not**
      the wallet's own hotkey. A workspace configured with someone else's
      hotkey announces under that hotkey, and it is that hotkey's owner the
      backend looks up;
    - an **empty** owner is a mismatch too. Empty means the chain holds no such
      mapping -- an unregistered hotkey, or one typed with a character wrong --
      and treating "no answer" as a pass is what lets anyone pay a fee against
      anyone else's hotkey. The backend reads it exactly this way and says so.

    Both ways of paying are guarded, not just the transfer: the burn's signer is
    the coldkey as well, so `judge_burn` puts it through the same lookup and a
    burned fee is *more* irrecoverable than a transferred one, not less.
    """
    hotkey = str(state.get("hotkey_ss58", ""))
    try:
        # An attribute access that reads coldkeypub.txt off the disk, raising
        # `KeyFileError` when it is not there (measured in `doctor`). The type
        # comes from the SDK and is not ours to depend on, so the refusal is
        # written against any failure to read it -- an address we could not read
        # is one we cannot compare, and this is not the gate to guess at.
        payer = str(getattr(wallet.coldkeypub, "ss58_address", "") or "")
    except Exception as exc:
        fail(
            f"Could not read this wallet's coldkey address, so there is no way "
            f"to tell whether it is allowed to pay for {hotkey[:12]}... "
            f"**Nothing was paid, nothing was sent on chain.**\n"
            f"   {exc}\n"
            f"   → run `openroboto doctor`, which checks the wallet files, then "
            f"`openroboto submit` again -- your upload is kept"
        )
        return False

    owner = str(subtensor.get_hotkey_owner(hotkey) or "")
    if owner != payer:
        fail(
            f"This wallet is not the owner of the hotkey this submission "
            f"announces under, so the subnet would reject the submission and "
            f"keep the fee. **Nothing was paid, nothing was sent on chain.**\n"
            f"   hotkey:            {hotkey}\n"
            f"   owned on chain by: {owner or '(no owner registered on chain)'}\n"
            f"   would pay from:    {payer}\n"
            f"   The backend looks the owner up on chain and compares it with "
            f"whoever signed the payment (`fee_payer_not_owner`); that rejection "
            f"is final and the fee is not refunded, which is why this stops "
            f"before it.\n"
            f"   → point miner.yaml at the wallet that owns this hotkey, or fix "
            f"`subnet.hotkey_ss58` to the hotkey this wallet owns "
            f"(`btcli wallet overview` lists both), then run `openroboto submit` "
            f"again -- your upload is kept and is not pushed a second time"
        )
        return False
    return True


def _ready_to_spend(
    settings: Settings, competition_id: int, state: dict[str, Any], *, doing: str
) -> bool:
    """The last look at the commitment before any money moves. Shared by both
    ways of paying, because the thing being protected is the same either way: a
    fee that has left the wallet and a payload that turns out not to encode.
    """
    settings.require_for_chain()

    # The track decides which fields the payload must carry.
    reasons = check_announce_ready(
        state, _competition_seq(settings), payload_track(settings)
    )
    if reasons:
        fail(f"Pre-chain self-check failed; **not** {doing}:")
        for reason in reasons:
            say(f"   • {reason}")
        return False
    say(
        f"✅ self-check passed | commitment payload "
        f"{payload_size(state, _competition_seq(settings))}/512 bytes"
    )
    return True


def _record(competition_id: int, state: dict[str, Any], receipt: BurnReceipt) -> None:
    """Write the payment proof into the checkpoint.

    The keys keep their `burn_` names on both tracks: they are what `announce`
    reads and what the encoder puts on chain as `b` / `bb`, and renaming them
    would strand every checkpoint written by an earlier release between the
    payment and the announcement -- the one gap where the fee is already gone.
    """
    state["burn_tx_hash"] = receipt.tx_hash
    state["burn_block"] = receipt.block_number
    state["step"] = "burn"
    state["status"] = "completed"
    save_state(competition_id, state)
