"""The per-round checkpoint file `state/round_N.json`.

train → upload → burn → announce are four commands that **can each fail
independently**, so the intermediate state must hit disk: training ran for six
hours, upload died on the network, and rerunning must not retrain from
scratch. The file format is exactly identical to what the old `miner.py` /
`rt.py` wrote -- after a miner who is mid-run upgrades the CLI, the
`state/round_1.json` they already have must still be readable as is.

The only change is **the directory location**: the old code put state in
`<repository directory>/state` (`os.path.dirname(__file__)`). Once installed
as a pip package that location is inside site-packages, so it became `./state`
relative to **the current working directory**. Miners were already typing
commands inside the repository directory, so in practice the path did not
change.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

STATE_DIR = Path("state")
"""Checkpoint directory, relative to the current working directory."""

DEFAULT_OUTPUT_ROOT = Path("./tmp/robot_train_vla_miner")
"""Root directory for training output. The name keeps the old default --
miners' scripts and systemd units have it written down."""


class StateError(Exception):
    """The checkpoint file is missing, or the round cannot be determined. The
    message must say what to do next."""


def state_path(round_num: int, base: Path = STATE_DIR) -> Path:
    return base / f"round_{round_num}.json"


def load_state(round_num: int, base: Path = STATE_DIR) -> dict[str, Any]:
    """Read one round's checkpoint. A missing file or a corrupt read both count
    as empty -- an empty state makes the upstream command start from
    scratch."""
    path = state_path(round_num, base)
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def save_state(round_num: int, state: dict[str, Any], base: Path = STATE_DIR) -> None:
    """Write one round's checkpoint. Creates the directory if it is absent."""
    base.mkdir(parents=True, exist_ok=True)
    state_path(round_num, base).write_text(
        json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def competition_id(state: dict[str, Any]) -> int | None:
    """Which season this round is being submitted to, or `None`.

    It is written by the step that **paid** -- the pre-payment check resolves it
    from the row the backend served at that moment -- and read back here by
    `announce`, the same way `burn_tx_hash` travels between two commands that
    can be run minutes apart. Keeping it in the checkpoint is what makes a bare
    `openroboto announce` after a failed pipeline carry the same `cid` the fee
    was paid under.

    `None` means the key is absent, which is every config written before
    competitions existed; on chain the `cid` key is then not written at all.
    """
    value = state.get("competition_id")
    return int(value) if value else None


def model_hash(state: dict[str, Any]) -> str | None:
    """The fingerprint of the weights this round uploaded, or `None`.

    🔴 An empty string is **not** passed on. On chain, `encode()` distinguishes
    `None` (key absent) from `""` (key present and empty), and `""` would both
    break byte compatibility with every older payload and pin a fingerprint that
    says "there were no weights". A checkpoint that somehow holds one is treated
    as not having a fingerprint at all, and `check_payload` then refuses the
    real track by name.
    """
    value = state.get("model_hash")
    return str(value) if value else None


def is_step_done(state: dict[str, Any], step: str) -> bool:
    """Whether a given step has already finished. The `step` and `status`
    fields are judged together; neither one may be omitted."""
    return state.get("step") == step and state.get("status") == "completed"


def resolve_round(explicit: int, base: Path = STATE_DIR) -> int:
    """Work out which round to operate on: an explicit `--round` wins,
    otherwise take the most recent round that finished.

    Raises:
        StateError: There is not a single completed checkpoint -- guessing the
            round here is guessing with the miner's money, so stop instead.
    """
    if explicit and explicit > 0:
        return explicit

    candidates: list[int] = []
    if base.is_dir():
        for entry in base.glob("round_*.json"):
            try:
                candidates.append(int(entry.stem.split("_", 1)[1]))
            except (IndexError, ValueError):
                continue

    for round_num in sorted(candidates, reverse=True):
        if load_state(round_num, base).get("status") == "completed":
            return round_num

    raise StateError(
        "Cannot determine the round automatically: no completed checkpoint "
        "under state/.\n"
        "  \u2192 pass `--round N` explicitly, or run `openroboto train` first"
    )


def resolve_output_dir(round_num: int, base: Path = STATE_DIR) -> str:
    """This round's model output directory. Use what the checkpoint recorded if
    it recorded anything, otherwise build it from the default rule."""
    recorded = load_state(round_num, base).get("round_output")
    if isinstance(recorded, str) and recorded:
        return recorded
    return str(DEFAULT_OUTPUT_ROOT / f"round_{round_num}")


def training_metrics(round_num: int, base: Path = STATE_DIR) -> dict[str, Any]:
    """The metrics produced by this round's training; they are uploaded to HF
    together with the model."""
    metrics = load_state(round_num, base).get("training_metrics")
    return metrics if isinstance(metrics, dict) else {}
