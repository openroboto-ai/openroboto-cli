"""`openroboto upload` -- push the training artifact to HuggingFace (the old
`rt.py upload`)."""

from __future__ import annotations

import argparse
from typing import Any

from openroboto_protocol.commitment import Track

from openroboto.config import ConfigError, Settings
from openroboto.console import say
from openroboto.huggingface import (
    UploadError,
    build_repo_id,
    fetch_model_hash,
    push_model,
)
from openroboto.preflight import payload_track
from openroboto.round_state import (
    load_state,
    resolve_output_dir,
    resolve_round,
    save_state,
    training_metrics,
)


def add_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("upload", help="upload the model to HuggingFace")
    parser.add_argument("--config", default="miner.yaml")
    parser.add_argument("--round", type=int, default=0)
    parser.add_argument("--output-dir", default="")
    parser.set_defaults(handler=run)


def run(args: argparse.Namespace) -> int:
    settings = Settings.load(args.config)
    round_num = resolve_round(args.round)
    output_dir = args.output_dir or resolve_output_dir(round_num)

    state = load_state(round_num)
    perform_upload(settings, round_num, output_dir, state)
    say(f"✅ uploaded: {state['hf_url']}")
    say(f"   commit={state['hf_commit'][:8]} repo={state['hf_repo_id']}")
    say("   → next: `openroboto burn` (it runs the pre-spend self-check first)")
    return 0


def perform_upload(
    settings: Settings,
    round_num: int,
    output_dir: str,
    state: dict[str, Any],
    reuse_existing: bool = False,
) -> None:
    """Upload and write the result into the checkpoint.

    With `reuse_existing=True`, an upload result already present in the
    checkpoint is reused directly -- `submit` takes this path, so a single
    pipeline does not re-upload several GB. Typing `openroboto upload` on its
    own **always re-uploads**: the miner re-exported the checkpoint and is
    uploading it again, and "skip" is the wrong answer at that moment.
    """
    uploaded = all(state.get(key) for key in ("hf_repo_id", "hf_url", "hf_commit"))
    if reuse_existing and uploaded:
        say(f"⏭️  already uploaded: {state['hf_url']}")
        return

    if not settings.hf_token:
        raise ConfigError(
            "huggingface.token is not configured -- uploading needs an HF token "
            "with write access\n"
            "  → create one at https://huggingface.co/settings/tokens, then put "
            "it in miner.yaml"
        )

    hotkey_ss58 = state.get("hotkey_ss58") or settings.hotkey_ss58
    repo_id = build_repo_id(settings, str(hotkey_ss58 or ""))
    result = push_model(
        model_dir=output_dir,
        repo_id=repo_id,
        hf_token=settings.hf_token,
        round_num=round_num,
        metrics=training_metrics(round_num),
    )

    state["hf_repo_id"] = repo_id
    state["hf_url"] = result.url
    state["hf_commit"] = result.commit_sha
    state["step"] = "upload"
    state["status"] = "completed"
    if hotkey_ss58:
        state["hotkey_ss58"] = hotkey_ss58
    _record_model_hash(settings, repo_id, result.commit_sha, state)
    save_state(round_num, state)


def _record_model_hash(
    settings: Settings, repo_id: str, commit_sha: str, state: dict[str, Any]
) -> None:
    """Ask HF for the fingerprint of what was just pushed -- **real track only**.

    Two things decide the shape of this:

    - it happens **after** the push and nowhere else. The fingerprint is made of
      the repository's LFS object hashes, and those are computed by HuggingFace
      while receiving the files; there is no such value in the local checkpoint
      directory, so "compute it before uploading" is not a slower option, it is
      not an option;
    - it happens **only on the real track**. The simulation track's repositories
      are public, so the backend computes the fingerprint itself and the `m` key
      is not written at all -- doing this for everyone would add a network call,
      and a way to fail, to a path that has worked for every miner so far.

    An empty fingerprint stops the run here. It means the repository holds no
    LFS object -- the pointers were pushed and the weights were not -- and the
    next thing to happen would be paying an entry fee for an empty repository.
    `check_payload` would refuse it later anyway, but by then with a message
    about hex digits rather than about the weights.
    """
    if payload_track(settings) is not Track.REAL:
        return
    fingerprint = fetch_model_hash(repo_id, commit_sha, settings.hf_token)
    if not fingerprint:
        raise UploadError(
            f"{repo_id} contains no LFS file at this commit, so there are no "
            f"weights to fingerprint.\n"
            f"  Usually this means only the pointer files were pushed, or the "
            f"directory that was uploaded held no model at all.\n"
            f"  → run `openroboto check` on the output directory, then "
            f"upload again. **Nothing has been paid.**"
        )
    state["model_hash"] = fingerprint
