"""Push the training artifacts to the miner's HuggingFace repository.

What is uploaded is **the directory as-is**. The evaluator only accepts a complete
checkpoint (`params/` or `model.safetensors` +
`assets/physical-intelligence/libero/norm_stats.json`); a bare LoRA adapter is
rejected outright at pre-check — which is why `openroboto check` should be run once
before uploading.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class UploadError(Exception):
    """Upload failed.

    In most cases this is an infrastructure problem (HF jitter, an expired token),
    not a problem with the model.
    """


@dataclass(frozen=True)
class UploadResult:
    """The result of one upload.

    Both fields go into the checkpoint file, and `commit_sha` ends up on chain.
    """

    url: str
    commit_sha: str


def commit_sha_from_url(url: str) -> str:
    """Extract the commit SHA from a `.../commit/<sha>` style URL.

    Returns an empty string if it cannot be extracted.
    """
    if "/commit/" not in url:
        return ""
    return url.split("/commit/", 1)[1][:40]


def push_model(
    model_dir: str,
    repo_id: str,
    hf_token: str,
    competition_id: int = 0,
    metrics: dict[str, Any] | None = None,
    base_model: str = "",
) -> UploadResult:
    """Push all of `model_dir` to `repo_id`, whatever its visibility.

    Returns the URL and the commit SHA.

    The old implementation threw `upload_folder` into a worker thread with a
    600-second timeout, and on timeout treated it as a failure and returned `None`.
    A complete π0.5 checkpoint is several GB; 600 seconds is simply not enough to
    transfer it on ordinary home bandwidth — so that timeout manufactured **fake
    failures**: the files were in fact still uploading, and the miner, seeing
    "upload failed", would rerun. The timeout is removed here, leaving it to
    `huggingface_hub`'s own chunked retries.
    """
    from huggingface_hub import HfApi, create_repo, upload_folder

    path = Path(model_dir)
    if not path.is_dir():
        raise UploadError(
            f"Model directory does not exist: {model_dir}\n"
            "  \u2192 run `openroboto train` first, or point at it with "
            "--output-dir"
        )

    _write_run_info(path, competition_id, metrics, base_model)

    total_bytes = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    file_count = sum(1 for f in path.rglob("*") if f.is_file())
    logger.info(
        "📤 Preparing to upload %s | %d files, %.1f MB",
        repo_id,
        file_count,
        total_bytes / 1024 / 1024,
    )

    try:
        # 🔴 Do not flip an existing repository's visibility. This used to call
        # `update_repo_visibility(..., "public")` unconditionally, which silently
        # published a miner's private weights on the first upload -- and
        # publishing is not undoable: whoever fetched them while the repo was
        # open still has them.
        #
        # A private repository is explicitly supported, not tolerated. The real
        # track's spec says so in as many words (`docs/specs/10` §2.5): "a
        # miner's HF repository **may stay private indefinitely**; it only has to
        # add the official account as a read-only collaborator." The backend
        # reads those repos with `settings.HF_READ_TOKEN` and reports the access
        # verdict per submission, so this client has nothing to guarantee here.
        #
        # `exist_ok=True` leaves an existing repo exactly as it is. A repo this
        # call creates gets huggingface_hub's own default -- which is public, and
        # is the miner's to change afterwards.
        create_repo(repo_id=repo_id, token=hf_token, repo_type="model", exist_ok=True)
        api = HfApi(token=hf_token)

        commit_url = upload_folder(
            folder_path=str(path),
            repo_id=repo_id,
            repo_type="model",
            token=hf_token,
            # Named after the season's base model, not after pi0.5. This message
            # lands in the miner's own commit history, where "pi0.5 LIBERO model"
            # over a LingBot checkpoint is a claim nobody made on purpose.
            commit_message=(
                f"Competition {competition_id}"
                f"{' ' + base_model if base_model else ''} model"
                f" — {datetime.now(UTC).isoformat()}"
            ),
        )
        commit_sha = commit_sha_from_url(str(commit_url)) or str(
            api.repo_info(repo_id, repo_type="model").sha
        )
    except Exception as exc:
        raise UploadError(f"Push to HF failed: {exc}") from exc

    url = (
        str(commit_url)
        if "/commit/" in str(commit_url)
        else f"https://huggingface.co/{repo_id}"
    )
    logger.info("✅ Uploaded %s | commit=%s", url, commit_sha[:8])
    return UploadResult(url=url, commit_sha=commit_sha)


def _write_run_info(
    path: Path,
    competition_id: int,
    metrics: dict[str, Any] | None,
    base_model: str = "",
) -> None:
    """Put a `run_info.json` into the model directory, uploaded with the model.

    `model` used to be the literal `"pi05"`, written next to a LingBot checkpoint
    just as readily as next to a pi0.5 one. This file ships inside the miner's
    repository, so the wrong value there is a claim about the artifact it sits in.

    An empty `base_model` writes `""` rather than guessing: the season names the
    base model (`competition.base_model_family`), and a workspace whose season has
    not named one has nothing true to put here.
    """
    info: dict[str, Any] = {
        "model": base_model,
        "competition_id": competition_id,
        "created_at": datetime.now(UTC).isoformat(),
        "client": f"openroboto-cli/{_client_version()}",
    }
    if metrics:
        info["metrics"] = {
            "final_loss": metrics.get("final_loss", 0),
            "action_mse": metrics.get("action_mse", 0),
            "training_steps": metrics.get("training_steps", 0),
            "training_duration_sec": metrics.get("training_duration_seconds", 0),
        }
    (path / "run_info.json").write_text(json.dumps(info, indent=2), encoding="utf-8")


def _client_version() -> str:
    from openroboto import __version__

    return __version__
