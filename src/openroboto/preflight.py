"""The self-check that runs before spending money.

Finding out only after the burn that the commitment cannot be sent = the TAO
is burned for nothing and is not refunded. So both `burn` and `submit` run
this pass **before spending anything**: are the fields that should be in the
checkpoint all there, can the payload be encoded, does it exceed 512 bytes.

The old `rt.py::check_announce_ready` treated `h` (the block hash) as an empty
string when estimating the payload size, while on chain it is 64 hexadecimal
characters -- meaning **it undercounted by 64 bytes every single time**. An
hf_repo_id sitting right on the boundary would sail through the preflight,
burn the TAO, and then blow up at the on-chain step. Here the estimate uses a
64-character placeholder hash, preferring to overestimate.
"""

from __future__ import annotations

from typing import Any

from openroboto_protocol.commitment import (
    CommitmentFieldError,
    CommitmentPayload,
    CommitmentTooLargeError,
    Track,
    check_payload,
    encode,
)

from openroboto import round_state
from openroboto.competition import load_snapshot
from openroboto.config.settings import Settings

HF_COMMIT_LEN = 40
BLOCK_HASH_PLACEHOLDER = "f" * 64
"""Placeholder block hash used for estimation, the same length as a real
one."""

COMMIT_LAG_BLOCKS = 5
"""Estimated margin (in blocks) between sending a commitment and it being
included in a block.

When only this much of the window is left, emit a warning first: the check is
performed at the **current** block, while the commitment is actually included
a few blocks later, so a submission sitting exactly on the boundary will
overrun the window on the backend's side. **It must not be folded into the
blocking decision** -- that would be stricter than the backend and would
reject submissions the backend would have accepted.
"""


def check_burn_window(
    burn_block: int, commit_block: int, window: int
) -> tuple[str, str]:
    """Is the block distance between burn and commitment still inside the
    backend's window?

    Returns `(blocking reason, warning)`; both being empty strings means
    everything is fine. **A pure function**: it does not touch the chain and
    does not print, keeping the decision separate from the presentation (the
    presentation lives in `commands/announce.py`).

    The decision is copied verbatim from the backend's
    `scanner/burn_verify.py:68-75`; three details must match, otherwise this
    check will block submissions that would have passed:

    1. `abs(burn_block - commit_block)`: **symmetric**, the distance counts
       whether the burn came before or after;
    2. it rejects only when `> window` -- being exactly equal to the window
       **passes**;
    3. when either block is 0 (unknown), the backend **skips this whole
       check**, and so does this function.
    """
    if burn_block <= 0 or commit_block <= 0:
        return "", ""  # the backend does not check either; never be stricter

    diff = abs(commit_block - burn_block)
    if diff > window:
        return (
            f"The burn is {diff} blocks old, past the backend window of {window} "
            f"-- announcing now will be `rejected`, and **the TAO you already "
            f"burned is not refunded**.\n"
            f"   The burn is in block {burn_block}, the current block is "
            f"{commit_block}.\n"
            f"   \u2192 this round's burn is wasted. Next time run `openroboto "
            f"submit` in one go, or announce immediately after burning -- do not "
            f"leave a long gap",
            "",
        )

    if diff > window - COMMIT_LAG_BLOCKS:
        return "", (
            f"The burn is {diff} blocks old and the window is {window} -- that is "
            f"right on the edge, and the commitment may land just past it once it "
            f"is included in a block. Do not wait any longer"
        )
    return "", ""


def payload_track(settings: Settings) -> Track:
    """Which track's rules this workspace's payload is judged by.

    A config with no competition section is the simulation track -- the same
    reading the chain side gives a commitment that carries no `cid`, and the
    promise `MIGRATION.md` §2 makes to configs written before competitions
    existed.

    A track string this client does not know **raises** (`ValueError` carrying
    the offending value) and never falls back to simulation. Falling back would
    put a real-track submission on the simulation leaderboard *after* the entry
    fee has been paid.
    """
    snapshot = load_snapshot(settings)
    if snapshot is None:
        return Track.SIM
    return Track(snapshot.track)


def check_announce_ready(
    state: dict[str, Any], round_num: int, track: Track = Track.SIM
) -> list[str]:
    """Return the list of reasons blocking the submission; an empty list means
    it is fine to continue.

    `track` decides which fields are required, and the decision itself is the
    protocol package's (`check_payload`) rather than a second copy written here:
    "what does the real track require" spelled out on both sides is how the two
    drift. It defaults to the simulation track, which is what a config with no
    competition section is.
    """
    reasons: list[str] = []

    hf_repo_id = str(state.get("hf_repo_id", ""))
    hf_url = str(state.get("hf_url", ""))
    hf_commit = str(state.get("hf_commit", ""))
    hotkey_ss58 = str(state.get("hotkey_ss58", ""))

    if not hf_repo_id:
        reasons.append(
            "No hf_repo_id in the checkpoint state -- run `openroboto upload` first"
        )
    if not hf_url:
        reasons.append(
            "No hf_url in the checkpoint state -- run `openroboto upload` first"
        )
    if len(hf_commit) != HF_COMMIT_LEN:
        shown = hf_commit[:12] if hf_commit else "empty"
        reasons.append(
            f"Invalid hf_commit ({shown}, expected 40 hexadecimal characters) "
            "-- run `openroboto upload` again"
        )
    if not hotkey_ss58:
        reasons.append(
            "No hotkey_ss58 in the checkpoint state -- "
            "add subnet.hotkey_ss58 to miner.yaml and run upload again"
        )

    if hf_repo_id and hotkey_ss58:
        try:
            payload_size(state, round_num)
        except CommitmentTooLargeError as exc:
            reasons.append(
                f"The commitment payload is too large ({exc.size} > 512 bytes) "
                "-- pick a shorter HF repo name"
            )

    # The field rules of the track being entered, checked **before the fee**.
    # On the simulation track this only repeats the commit format above; on the
    # real track it is the gate that stops a fee being paid for a submission the
    # backend will refuse for a missing `cid` or a malformed fingerprint.
    try:
        check_payload(_estimated_payload(state, round_num), track)
    except CommitmentFieldError as exc:
        reasons.append(f"{_FIELD_ADVICE.get(exc.field, str(exc))} ({exc})")

    return reasons


#: What to do about each on-chain key `check_payload` can reject. Keyed by the
#: **chain key name** the protocol package reports, so a new required field
#: surfaces as its raw message rather than being silently mapped to the wrong
#: advice.
_FIELD_ADVICE = {
    "c": "The HF commit in the checkpoint is not a commit SHA -- run "
    "`openroboto upload` again",
    "cid": "This workspace mines a real-track competition, but the checkpoint "
    "does not say which season the fee is for -- run `openroboto submit`, "
    "which resolves it from the backend, rather than `openroboto burn` on "
    "its own",
    "m": "The model fingerprint for this round is missing or malformed. The "
    "real track needs it on chain because the repository may be private, so "
    "the evaluator cannot compute it later -- run `openroboto upload` again",
}


def _estimated_payload(state: dict[str, Any], round_num: int) -> CommitmentPayload:
    """The payload as it would go on chain, with a placeholder block hash.

    `competition_id` / `model_hash` are read from the checkpoint, so the size
    below counts the keys this submission will actually carry: a legacy config
    has neither and estimates exactly what it always did, while a real-track one
    pays for both keys here rather than at the on-chain step, when the fee is
    already gone.
    """
    return CommitmentPayload(
        hotkey_ss58=str(state.get("hotkey_ss58", "")),
        block_hash=BLOCK_HASH_PLACEHOLDER,
        hf_commit=str(state.get("hf_commit", "")),
        round_num=round_num,
        hf_repo_id=str(state.get("hf_repo_id", "")),
        burn_tx_hash=str(state.get("burn_tx_hash", "")) or "0" * 64,
        burn_block=int(state.get("burn_block", 0) or 0) or 1,
        competition_id=round_state.competition_id(state),
        model_hash=round_state.model_hash(state),
    )


def payload_size(state: dict[str, Any], round_num: int) -> int:
    """Estimate the byte size of the on-chain payload (using the placeholder
    block hash and the burn fields)."""
    return len(encode(_estimated_payload(state, round_num)))
