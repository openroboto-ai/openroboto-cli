"""`openroboto status` -- look up submission status and rejection reasons.

Both endpoints need **no API key** (measured 2026-08-17):

- `/api/v1/submissions/history` -- the status after a submission has entered
  the queue;
- `/api/v1/scan-rejections`     -- why a submission was rejected already at
  the chain-scan stage (burn block too old, wrong amount, model hash
  collision, ...).

"It went on chain but there is nothing in the queue" is answered by the second
endpoint -- the question miners ask most often, which previously could only be
answered by curling by hand.

When a rejection record carries a `reason`, two extra lines are printed: the
stable error code, and **whether to burn another TAO and retry**. "The
infrastructure flapped" and "your model format is wrong" must be
distinguishable here at a glance -- guessing wrong is paid for by the miner.
"""

from __future__ import annotations

import argparse
from datetime import datetime
from typing import TypeVar

from openroboto_protocol.schemas import (
    Competition,
    Reason,
    ScanRejection,
    SubmissionHistoryItem,
)
from openroboto_protocol.status import normalize_status

from openroboto.backend_api import (
    BackendError,
    fetch_competitions,
    fetch_rejections,
    fetch_roster,
    fetch_submissions,
    retry_advice,
)
from openroboto.competition import Snapshot, load_snapshot
from openroboto.config import ConfigError, Settings
from openroboto.console import say

DEFAULT_LIMIT = 10
#: One request instead of a paging loop. It is the backend's maximum, and a
#: season with more entries than this has other problems.
ROSTER_LIMIT = 1000

_Row = TypeVar("_Row", SubmissionHistoryItem, ScanRejection)


def add_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "status", help="Look up submission status and rejection reasons"
    )
    parser.add_argument("--config", default="miner.yaml")
    parser.add_argument(
        "--hotkey", default="", help="hotkey SS58; defaults to the one in miner.yaml"
    )
    parser.add_argument("--round", type=int, default=0, help="Only show this round")
    parser.add_argument(
        "--limit", type=int, default=DEFAULT_LIMIT, help="Max rows shown per section"
    )
    parser.set_defaults(handler=run)


def run(args: argparse.Namespace) -> int:
    settings = _load_settings(args.config)
    hotkey = args.hotkey or settings.hotkey_ss58
    if not hotkey:
        raise ConfigError(
            "Don't know whose submissions to look up -- pass `--hotkey <SS58>`, "
            "or set subnet.hotkey_ss58 in miner.yaml"
        )

    say(f"backend: {settings.backend_url}")
    say(f"hotkey: {hotkey}")
    say("")

    history = fetch_submissions(settings.backend_url, hotkey, args.limit)
    submissions = _by_round(history.data, args.round)
    say(f"Submissions ({len(submissions)})")
    if not submissions:
        say("  (No records. If you just ran announce, wait one chain-scan cycle.)")
    for row in submissions:
        say(
            f"  round={row.round_num} "
            f"status={display_status(row)} "
            f"repo={row.hf_repo_id or '?'} "
            f"commit_block={row.commit_block} "
            f"submitted_at={_when(row.submitted_at)}"
        )
    say_more_hint(history.meta.page.has_more, history.meta.page.total)

    rejected = fetch_rejections(settings.backend_url, hotkey, args.limit)
    rejections = _by_round(rejected.data, args.round)
    say("")
    say(f"Rejected during chain scan ({len(rejections)})")
    if not rejections:
        say("  (No rejections.)")
    for rejection in rejections:
        say(f"  round={rejection.round_num} burn_block={rejection.burn_block}")
        say(f"    reason: {rejection.reject_reason or '?'}")
        for line in explain(rejection.reason):
            say(f"    {line}")
    say_more_hint(rejected.meta.page.has_more, rejected.meta.page.total)
    if rejections:
        say("")
        say(
            "A rejected burn is not refunded. Fix the reason, then run "
            "`openroboto submit` again (it burns a new one)."
        )

    say_roster(settings, hotkey)
    return 0


def say_roster(settings: Settings, hotkey: str) -> None:
    """Where this miner stands in their competition's entry list.

    Only printed for a workspace that mines a specific competition, and only
    when this hotkey is actually on that list -- an empty table under a heading
    tells a miner nothing they did not already know.

    **Never fatal.** This is the troubleshooting command: a backend too old to
    serve the endpoint, or a competition that has gone away, must not take the
    submission and rejection sections down with it. Those two are what the miner
    came here for.
    """
    snapshot = load_snapshot(settings)
    if snapshot is None:
        return

    try:
        live = _live_competition(settings, snapshot)
        roster = fetch_roster(settings.backend_url, live.id, limit=ROSTER_LIMIT)
    except BackendError:
        say("")
        say(f"{snapshot.label}: this backend cannot answer entry-list queries yet")
        return

    say("")
    say(f"{live.label} ({live.track}/{live.seq} · cid={live.id})")
    mine = [row for row in roster.data if row.hotkey == hotkey]
    if not mine:
        say("  You are not on the entry list yet.")
        return

    # The list arrives newest first. Counting from the far end therefore counts
    # in the order entries were **joined**, which is the order they are worked
    # -- and the miner's own earliest entry is the last of theirs in the list.
    entry = mine[-1]
    place = roster.meta.page.total - roster.data.index(entry)
    say(f"  {hotkey[:8]}… is #{place} of {roster.meta.page.total} by submission time")
    say(f"  payment: {entry.payment_status} | HF access: {entry.hf_access_status}")
    if entry.invalid_reason:
        say(f"  ⚠️  {entry.invalid_reason} — this entry cannot be evaluated as it is")
    if roster.meta.page.has_more:
        # One page is 1000 entries. Saying the number anyway would be saying a
        # number computed from part of the list.
        say(
            f"  ⚠️  more than {ROSTER_LIMIT} entries; the place above counts only "
            f"the {ROSTER_LIMIT} most recent"
        )


def _live_competition(settings: Settings, snapshot: Snapshot) -> Competition:
    """Resolve the workspace's competition against the backend, by `(track,
    seq)` -- the id in `miner.yaml` is local to one database."""
    for row in fetch_competitions(settings.backend_url, include_archived=True).data:
        if row.track == snapshot.track and row.seq == snapshot.seq:
            return row
    raise BackendError(f"{settings.backend_url} does not list {snapshot.name}")


def explain(reason: Reason | None) -> list[str]:
    """Flatten one `reason` into the two lines a miner can act on.

    `code` is the stable machine code (scripts branch on it), and `retryable`
    answers "do I have to burn another one". The old `reject_reason` field is
    still printed on the line above -- this is an addition, not a replacement.
    """
    if reason is None:
        return []
    return [
        f"error code: {reason.code} (from the {reason.source} stage)",
        retry_advice(reason.retryable),
    ]


def say_more_hint(has_more: bool, total: int) -> None:
    """Say so when there are records that were not displayed.

    `has_more` is computed by the backend (`meta.page`); we no longer derive it
    again here from `offset + len(rows) < total` -- every copy of that
    expression is one more chance to get it wrong, and getting it wrong shows
    up as "the miner believes they only submitted this many times".
    """
    if has_more:
        say(f"  ({total} in total, only the first few shown -- raise `--limit`)")


def display_status(row: SubmissionHistoryItem) -> str:
    """Map the status word returned by the backend onto the protocol
    vocabulary.

    Reads `eval_status` only. **The old `status` column does not appear here,
    because the model does not have it at all** -- the two columns disagree on
    52 rows, and reading `status` first is exactly the root cause of the 33
    out of 95 records that historically displayed the wrong status.

    TODO(blocking issue ①): the worker only knows `done` / `scored` /
    `failed`, while the backend gives `evaluated` / `eval_failed`. Here we
    display uniformly using the protocol's vocabulary; once both sides settle
    it, if a reverse conversion is needed (protocol word → worker word), add
    it next to this function and do not scatter it across the call sites.
    """
    return normalize_status(row.eval_status) if row.eval_status else "?"


def _when(moment: datetime | None) -> str:
    return moment.isoformat() if moment is not None else "?"


def _by_round(rows: list[_Row], round_num: int) -> list[_Row]:
    if not round_num:
        return rows
    return [row for row in rows if row.round_num == round_num]


def _load_settings(path: str) -> Settings:
    """Querying must work even when the config file cannot be read -- this
    command is for troubleshooting and must not be blocked by a config
    problem."""
    try:
        return Settings.load(path)
    except ConfigError:
        return Settings()
