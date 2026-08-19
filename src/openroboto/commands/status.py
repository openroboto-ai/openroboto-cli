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

from openroboto_protocol.schemas import Reason, ScanRejection, SubmissionHistoryItem
from openroboto_protocol.status import normalize_status

from openroboto.backend_api import fetch_rejections, fetch_submissions, retry_advice
from openroboto.config import ConfigError, Settings
from openroboto.console import say

DEFAULT_LIMIT = 10

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
    return 0


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
