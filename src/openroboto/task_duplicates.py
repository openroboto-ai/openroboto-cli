"""Ask the backend whether a scene task's problem is already full in the pool.

The pool counts at most `cap` tasks per problem; a task submitted after that is
rejected as a duplicate and its fee is not refunded. `check` reports the
verdict; `submit` refuses to pay on `full` unless told to. The query is advisory
when it cannot run: our service being down must never block a miner.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from openroboto.backend_api import (
    check_task_duplicates,
    fetch_task_access_message,
    miner_key,
)
from openroboto.config import Settings


def query(settings: Settings, package: Path) -> dict[str, Any]:
    """The backend verdict for `package`'s execution task.json.

    Raises:
        DuplicateCheckUnavailable: no backend, no signing wallet, or the backend
            could not answer (rate limit, model unavailable, network).
    """
    from openroboto.chain import hotkey_signature, open_wallet
    from openroboto.competition import workspace_competition_id

    task_json = package / "execution" / "task.json"
    if not settings.backend_url:
        raise DuplicateCheckUnavailable("this workspace names no backend")
    try:
        task = json.loads(task_json.read_text(encoding="utf-8"))
        wallet = open_wallet(settings)
        hotkey = str(wallet.hotkey.ss58_address)
        message = fetch_task_access_message(settings.backend_url, hotkey)
        return check_task_duplicates(
            settings.backend_url,
            hotkey=hotkey,
            signature=hotkey_signature(wallet, message.encode("utf-8")),
            task=task,
            competition_id=workspace_competition_id(settings),
            miner_key=miner_key(settings),
        )
    except Exception as exc:  # advisory: wallet, network and backend errors alike
        reason = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
        raise DuplicateCheckUnavailable(reason) from exc


class DuplicateCheckUnavailable(Exception):
    """The duplicate query did not run; callers warn and continue."""


def describe(result: dict[str, Any]) -> str:
    """One English line a miner can act on."""
    problem = result.get("query_problem") or "this problem"
    cap = result.get("cap")
    same = result.get("same_problem", 0)
    if result["verdict"] == "full":
        return (
            f"Duplicate check: FULL. The task pool already counts {same} tasks "
            f"for the same problem (limit {cap}): {problem}. Submitted now, this "
            "task is rejected as a duplicate and the submission fee is not "
            "refunded. Change the core problem, not only objects, sizes or layout."
        )
    if result["verdict"] == "crowded":
        return (
            f"Duplicate check: {same} of {cap} slots for the same problem are "
            f"taken: {problem}. This task still counts if it is admitted before "
            "the limit is reached."
        )
    return "Duplicate check: no task in the pool solves the same problem."
