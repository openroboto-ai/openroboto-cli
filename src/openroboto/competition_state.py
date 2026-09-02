"""The workspace's checkpoint file, `state/competition_<id>.json`.

train → upload → pay → announce are four steps that **can each fail
independently**, so the intermediate state has to hit disk: training ran for six
hours, the upload died on the network, and rerunning must not retrain from
scratch.

One file per competition, named after the competition the workspace was
initialised for (`competition.id` in `miner.yaml`). A workspace mines one
season, so the id is the whole key: training again writes over the same file,
the same way the season's HuggingFace repository is pushed over.

🔴 **`competition.id` is a local bookkeeping key, not the id that goes on
chain.** It is local to whichever backend database served it, which is why
`competition.resolve_competition()` re-resolves the season by `(track, seq)` at
payment time and writes *that* id into the checkpoint under `competition_id`
(read back by `paid_competition_id`). Naming a file is the one job the id in
`miner.yaml` is good enough for.

The directory is `./state`, relative to **the current working directory** --
installed as a pip package, anything relative to the module lives inside
site-packages.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from openroboto.huggingface.upload import commit_sha_from_url

STATE_DIR = Path("state")
"""Checkpoint directory, relative to the current working directory."""

DEFAULT_OUTPUT_ROOT = Path("./tmp/robot_train_vla_miner")
"""Root directory for training output."""


class StateError(Exception):
    """The workspace cannot say which competition it is operating on. The
    message must say what to do next."""


def state_path(competition_id: int, base: Path = STATE_DIR) -> Path:
    return base / f"competition_{competition_id}.json"


def load_state(competition_id: int, base: Path = STATE_DIR) -> dict[str, Any]:
    """Read this competition's checkpoint. A missing file or a corrupt read both
    count as empty -- an empty state makes the calling command start from
    scratch."""
    path = state_path(competition_id, base)
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def save_state(
    competition_id: int, state: dict[str, Any], base: Path = STATE_DIR
) -> None:
    """Write this competition's checkpoint. Creates the directory if absent."""
    base.mkdir(parents=True, exist_ok=True)
    state_path(competition_id, base).write_text(
        json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def paid_competition_id(state: dict[str, Any]) -> int | None:
    """The competition id this submission was **paid** under, or `None`.

    It is written by the step that paid -- the pre-payment check resolves it
    from the row the backend served at that moment -- and read back here by
    `announce`, the same way `burn_tx_hash` travels between two steps that can
    run minutes apart. Keeping it in the checkpoint is what makes an `announce`
    after a failed pipeline carry the same `cid` the fee was paid under.

    🔴 **Not the number the file is named after.** That one comes from
    `miner.yaml`; this one was resolved against the backend seconds before the
    money moved, and it is the only one allowed on chain.
    """
    value = state.get("competition_id")
    return int(value) if value else None


def announced_commit(state: dict[str, Any]) -> str:
    """The HF commit this submission pins on chain -- `""` if there is none.

    The URL wins because what the upload returns *is* that commit; the
    `hf_commit` key is the fall back for a URL with no commit segment, which is
    what `repo_info().sha` leaves behind. Without a commit the backend cannot
    verify the model, which is a fee paid for a submission nobody can score.

    It lives here, next to the other checkpoint readers, because **two** steps
    need the same answer: `announce` puts it on chain, and the layout gate in
    `submit` judges the repository *at that revision* before the fee is paid. A
    gate that judged some other revision would be theatre, and two copies of a
    one-line expression is exactly how that happens.
    """
    return commit_sha_from_url(str(state.get("hf_url", ""))) or str(
        state.get("hf_commit", "")
    )


def model_hash(state: dict[str, Any]) -> str | None:
    """The fingerprint of the weights that were uploaded, or `None`.

    🔴 An empty string is **not** passed on. On chain, `encode()` distinguishes
    `None` (key absent) from `""` (key present and empty), and `""` would pin a
    fingerprint that says "there were no weights". A checkpoint that somehow
    holds one is treated as not having a fingerprint at all, and `check_payload`
    then refuses the real track by name.
    """
    value = state.get("model_hash")
    return str(value) if value else None


def is_step_done(state: dict[str, Any], step: str) -> bool:
    """Whether a given step has already finished. The `step` and `status`
    fields are judged together; neither one may be omitted."""
    return state.get("step") == step and state.get("status") == "completed"


def resolve_output_dir(competition_id: int, base: Path = STATE_DIR) -> str:
    """This competition's model output directory. Use what the checkpoint
    recorded if it recorded anything, otherwise build it from the default
    rule."""
    recorded = load_state(competition_id, base).get("output_dir")
    if isinstance(recorded, str) and recorded:
        return recorded
    return str(DEFAULT_OUTPUT_ROOT / f"competition_{competition_id}")


def training_metrics(competition_id: int, base: Path = STATE_DIR) -> dict[str, Any]:
    """The metrics produced by this competition's training; they are uploaded to
    HuggingFace together with the model."""
    metrics = load_state(competition_id, base).get("training_metrics")
    return metrics if isinstance(metrics, dict) else {}
