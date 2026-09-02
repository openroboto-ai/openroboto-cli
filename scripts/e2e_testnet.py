#!/usr/bin/env python3
"""End-to-end run of the miner flow against Bittensor testnet and a live backend.

Covers steps 1-5 of the release gate (`openroboto-backend`'s
`docs/上线验收计划.md` S1): check -> submit -> the chain scanner admits it.
`submit` is one command that does upload, the season check, the layout gate, the
fee and the chain announcement; `upload` / `burn` / `announce` stopped being
commands in 1.0. Steps 6-10 need a GPU worker to actually evaluate the model, so
they are out of scope here.

## 🔴 It cannot complete unattended today, and that is not a bug here

`submit` asks `Pay now? [y/N]` and `competition._confirmed()` answers **no** for
anything that is not a tty -- "there is no silent yes on a path that spends
money". `run_cli` uses `subprocess.run`, so under `workflow_dispatch` or cron
stdin is not a tty and step 2 stops at the prompt with nothing paid and the
upload already done (which is the correct, cheap outcome: re-running resumes).

Run it **from a terminal** and answer the prompt, and the whole gate passes.

The docstring of `_confirmed()` still says a script that wants to pay "has to
call the single-step commands" -- those commands were removed in 1.0, so that
escape route no longer exists. Giving this script a pty and typing `y` into it
would be exactly the `--skip-*` hatch the CLI refuses to have, only spelled
differently, so it is **not** done here. Making the release gate unattended needs
an explicit decision about how a machine confirms a payment; until then the
scheduled trigger in `.github/workflows/e2e-testnet.yml` stays commented out.

**Deliberately not collected by pytest.** `AGENTS.md` §4 says any skip in CI is
red, on the grounds that the unit suite needs no GPU, chain or network. This
script needs all but the GPU, so it is not a unit test -- it is a release gate.
Run it on a schedule or before a release, not on every pull request: a flaky
chain RPC turning someone's PR red teaches people to ignore red.

Every step drives the real `openroboto` entry point through a subprocess rather
than importing it, so exit codes are covered too -- miners chain these commands
as `check && submit`, and an exit code that disagrees with the printed verdict
is what sends them on to pay for a checkpoint the backend will reject.

## Configuration (all via environment)

    E2E_HF_TOKEN          HuggingFace token with write + create-repo
    E2E_HF_USERNAME       HuggingFace account name
    E2E_BACKEND_URL       backend base URL, e.g. http://127.0.0.1:8000
    E2E_NETUID            defaults to 313
    E2E_NETWORK           defaults to "test"
    E2E_WALLET_NAME       local wallet name (developer machines)
    E2E_COLDKEY_MNEMONIC  coldkey mnemonic (CI; takes precedence)
    E2E_HOTKEY_MNEMONIC   hotkey mnemonic (CI; required with the above)
    E2E_KEEP_HF_REPO      set to 1 to keep the uploaded repo for inspection

The competition is **not** configured here: it is fetched from
`GET /api/v1/competitions` on the backend above and written into `miner.yaml` by
the same function `openroboto init` uses. `submit` refuses a workspace with no
`competition:` section.

The two mnemonics exist so CI can rebuild the wallet from secrets. They are
written to a temporary wallet directory that is removed on exit.

## What it costs

One entry fee per run, at whatever the season charges (`params.fee.amount_tao`
on the competition row this script picks), plus transaction fees. Testnet TAO,
but not free -- a loop that reruns this on every push will drain the wallet and
then fail on an empty balance, which reads like a code failure and is not one.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

MIN_CHECKPOINT_BYTES = 12 * 1024 * 1024  # the protocol floor is 10 MB
SCAN_TIMEOUT_SEC = 240  # the scanner's cycle is 60s; allow three plus slack
SCAN_POLL_SEC = 15


class E2EError(Exception):
    """A step failed. The message is the whole report -- no stack trace needed."""


def env(name: str, default: str = "") -> str:
    value = os.environ.get(name, default)
    if not value and default == "":
        raise E2EError(f"{name} is not set; see this script's docstring")
    return value


def step(number: int, title: str) -> None:
    print(f"\n{'─' * 62}\n[{number}] {title}\n{'─' * 62}", flush=True)


def run_cli(args: list[str], workspace: Path) -> subprocess.CompletedProcess[str]:
    """Invoke the installed `openroboto` entry point, echoing its output."""
    proc = subprocess.run(
        ["openroboto", *args],
        cwd=workspace,
        capture_output=True,
        text=True,
        timeout=900,
    )
    print(proc.stdout, end="")
    if proc.stderr:
        print(proc.stderr, end="", file=sys.stderr)
    return proc


def build_checkpoint(directory: Path) -> None:
    """A checkpoint that is structurally valid and evaluates to nothing.

    Random bytes, not zeros: a sparse file of zeros would be a fine input to the
    layout check but would compress to nothing on upload, so the run would not
    exercise the transfer path it is here to exercise.
    """
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "model.safetensors").write_bytes(os.urandom(MIN_CHECKPOINT_BYTES))
    stats = directory / "assets/physical-intelligence/libero/norm_stats.json"
    stats.parent.mkdir(parents=True, exist_ok=True)
    stats.write_text(
        json.dumps({"norm_stats": {"state": {"mean": [0.0], "std": [1.0]}}}),
        encoding="utf-8",
    )
    # Every real HF model repo has one; an earlier revision rejected 51 of 51
    # valid submissions over it, so keep it in the fixture.
    (directory / ".gitattributes").write_text(
        "*.safetensors filter=lfs diff=lfs merge=lfs -text\n", encoding="utf-8"
    )


def resolve_wallet(wallet_root: Path) -> tuple[str, str]:
    """Return (wallet_name, hotkey_ss58), rebuilding from mnemonics when CI
    supplies them. Falls back to a wallet that already exists on this machine."""
    import bittensor as bt

    coldkey_mnemonic = os.environ.get("E2E_COLDKEY_MNEMONIC", "")
    if not coldkey_mnemonic:
        name = env("E2E_WALLET_NAME")
        wallet = bt.Wallet(name=name, hotkey="default")
        return name, wallet.hotkey.ss58_address

    hotkey_mnemonic = os.environ.get("E2E_HOTKEY_MNEMONIC", "")
    if not hotkey_mnemonic:
        raise E2EError("E2E_COLDKEY_MNEMONIC is set but E2E_HOTKEY_MNEMONIC is not")

    wallet = bt.Wallet(name="e2e", hotkey="default", path=str(wallet_root))
    wallet.regenerate_coldkey(
        mnemonic=coldkey_mnemonic, use_password=False, overwrite=True, suppress=True
    )
    wallet.regenerate_hotkey(
        mnemonic=hotkey_mnemonic, use_password=False, overwrite=True, suppress=True
    )
    return "e2e", wallet.hotkey.ss58_address


def pick_competition(backend: str) -> Any:
    """The season this run submits to: the first one the backend serves.

    Asked rather than hard-coded, for the same reason `openroboto init` asks --
    the fee, the base model and the layout rules are the season's data, and a
    copy here would be a second source that goes stale silently. Unreachable is
    a hard stop, never a default.
    """
    from openroboto.backend_api import fetch_competitions

    rows = list(fetch_competitions(backend).data)
    if not rows:
        raise E2EError(
            f"{backend} lists no competition taking submissions right now, so "
            f"there is no season to submit to and nothing to test."
        )
    return rows[0]


def write_config(
    workspace: Path, hotkey_ss58: str, wallet_name: str, competition: Any
) -> Path:
    """Generate miner.yaml from the environment plus one live competition row.

    🔴 **The `competition:` section is not optional.** `openroboto submit`
    refuses a workspace without it before it uploads anything
    (`commands/submit.py::_no_season`), and `huggingface/repository.py` cannot
    build a repository name without `base_model_family`. It is rendered by
    `commands/init.render_section`, the same function `openroboto init` uses, so
    this file cannot drift from what a real miner's workspace looks like.

    `environment: dev` -- the preset for testnet 313 against
    `api-dev.openroboto.ai`, which is exactly what this script drives.

    It used to write `environment: local`, on the reasoning that local is the
    only preset that pins no network. That was true and still failed, because
    `local` also refuses to point at a hosted host: "environment=local, yet
    backend.url points at a hosted environment" is a contradiction, and
    `check_coherent()` said so before spending anything (2026-08-21, the first
    real run of this workflow -- it failed before paying with the HF repo
    already uploaded, and cleaned that repo up on the way out).

    That refusal was correct. The fix is to name the environment we are actually
    in, not to weaken the check.
    """
    from openroboto.commands.init import render_section

    # `source` is the backend the row came from; `check_coherent()` compares it
    # against `backend.url`, so passing anything else here is refused.
    section = render_section(competition, env("E2E_BACKEND_URL"))
    config = workspace / "miner.yaml"
    config.write_text(
        "environment: dev\n"
        "subnet:\n"
        f"  network: {env('E2E_NETWORK', 'test')}\n"
        f"  netuid: {env('E2E_NETUID', '313')}\n"
        f"  coldkey: {wallet_name}\n"
        "  hotkey: default\n"
        f'  hotkey_ss58: "{hotkey_ss58}"\n'
        f'  wallet_path: "{os.environ.get("E2E_WALLET_PATH", "")}"\n'
        "backend:\n"
        f'  url: "{env("E2E_BACKEND_URL")}"\n'
        "huggingface:\n"
        f'  token: "{env("E2E_HF_TOKEN")}"\n'
        f'  username: "{env("E2E_HF_USERNAME")}"\n'
        "log_level: INFO\n" + section,
        encoding="utf-8",
    )
    return config


def poll_for_submission(
    backend: str, competition_id: int, hotkey: str, burn_tx: str
) -> dict[str, object]:
    """Wait for the chain scanner to admit the submission we just announced.

    Matches on the payment transaction hash rather than "the newest row": a wallet
    is reused across runs, so it already carries older rows, and taking the
    newest one would let a scanner that never ran pass the step by reporting the
    previous run's success. The burn hash is unique per run and is the one value
    that appears both in the local checkpoint and on the scanner's row.
    """
    from openroboto.backend_api import fetch_submissions

    deadline = time.monotonic() + SCAN_TIMEOUT_SEC
    seen: list[object] = []
    while time.monotonic() < deadline:
        envelope = fetch_submissions(backend, competition_id, hotkey=hotkey, limit=20)
        for row in envelope.data:
            record = row.model_dump() if hasattr(row, "model_dump") else dict(row)
            if str(record.get("burn_tx_hash", "")).lower() == burn_tx.lower():
                return record
            seen.append(record.get("burn_tx_hash"))
        print(f"   … not scanned yet, retrying in {SCAN_POLL_SEC}s", flush=True)
        time.sleep(SCAN_POLL_SEC)

    raise E2EError(
        f"the scanner did not pick up burn_tx={burn_tx[:18]}… within "
        f"{SCAN_TIMEOUT_SEC}s.\n"
        f"  hashes it did report: {seen or 'none'}\n"
        "  → is `openroboto-ingest` running, and is it pointed at this netuid?"
    )


def main() -> int:
    workspace = Path(tempfile.mkdtemp(prefix="openroboto-e2e-"))
    wallet_root = workspace / "wallets"
    backend = env("E2E_BACKEND_URL")
    repo_id = ""
    hf_token = env("E2E_HF_TOKEN")

    try:
        step(0, "Environment")
        wallet_name, hotkey = resolve_wallet(wallet_root)
        if os.environ.get("E2E_COLDKEY_MNEMONIC"):
            os.environ["E2E_WALLET_PATH"] = str(wallet_root)
        competition = pick_competition(backend)
        config = write_config(workspace, hotkey, wallet_name, competition)
        print(
            f"workspace:   {workspace}\nhotkey:      {hotkey}\n"
            f"backend:     {backend}\ncompetition: {competition.label!r} "
            f"({competition.track}/{competition.seq}, cid={competition.id})"
        )

        step(1, "check -- the format verdict and its exit code")
        checkpoint = workspace / "checkpoint"
        build_checkpoint(checkpoint)
        result = run_cli(["check", str(checkpoint)], workspace)
        if result.returncode != 0:
            raise E2EError(
                "`check` rejected a fixture built to its own rules "
                f"(exit {result.returncode}).\n"
                "  → either the fixture or the layout rules moved; they disagree."
            )

        # Derived through the function the CLI itself uses, so the cleanup in
        # `finally` deletes the repository that was really created. A second
        # copy of the naming rule here is how the 1.2.0 rename left this script
        # deleting `<user>/pi05-…`, a repository that no longer exists, while
        # the real one was silently kept.
        from openroboto.config import Settings
        from openroboto.huggingface.repository import build_repo_id

        repo_id = build_repo_id(Settings.load(str(config)), hotkey)

        # One command: upload, the season check against the backend, the layout
        # gate on the HF listing, the duplicate-entry check, the fee, and the
        # chain commitment -- in that order, with nothing slow between the
        # payment and the announcement (the backend's window is 50 blocks).
        step(2, "submit -- upload, pay the entry fee, announce on chain")
        result = run_cli(
            [
                "submit",
                "--config",
                str(config),
                "--output-dir",
                str(checkpoint),
            ],
            workspace,
        )
        if result.returncode != 0:
            raise E2EError("`submit` failed; see the output above")

        step(3, "scan -- the backend admits the submission")
        state = json.loads(
            (workspace / f"state/competition_{competition.id}.json").read_text(
                encoding="utf-8"
            )
        )
        burn_tx = str(state.get("burn_tx_hash") or "")
        if not burn_tx:
            raise E2EError(
                "the local checkpoint carries no burn_tx_hash, so there is "
                "nothing to match the scanner's row against (the key holds the "
                "payment tx for both fee kinds)"
            )
        record = poll_for_submission(backend, competition.id, hotkey, burn_tx)
        print(f"   scanner row: {json.dumps(record, default=str)[:400]}")

        status = str(record.get("eval_status", ""))
        if status == "rejected":
            raise E2EError(
                f"the submission was admitted but rejected: "
                f"{record.get('reject_reason') or '(no reason given)'}\n"
                "  → if this is BURN_INSUFFICIENT, the amount the backend "
                "expects and `params.fee.amount_tao` on the competition row "
                "disagree. `submit` confirms the fee against that row before "
                "paying, so the two can only differ if the backend checks "
                "against something other than the row it serves."
            )
        if status != "pending":
            raise E2EError(f"expected eval_status=pending, got {status!r}")
        for field in ("seed", "block_hash"):
            if not record.get(field):
                raise E2EError(
                    f"{field} is empty on the admitted row -- the evaluation "
                    "seed is derived from it, so an empty value strands the "
                    "submission in the queue"
                )

        print("\n✅ steps 1-5 of the release gate passed")
        print(
            "   steps 6-10 (worker evaluation, scoring, weights on chain) need "
            "a GPU worker and are not covered here"
        )
        return 0

    except E2EError as exc:
        print(f"\n❌ {exc}", file=sys.stderr)
        return 1
    finally:
        if repo_id and not os.environ.get("E2E_KEEP_HF_REPO"):
            try:
                from huggingface_hub import HfApi

                HfApi(token=hf_token).delete_repo(repo_id=repo_id, repo_type="model")
                print(f"(cleaned up {repo_id})")
            except Exception as exc:  # cleanup must never mask the run's verdict
                print(f"(could not delete {repo_id}: {exc})", file=sys.stderr)
        shutil.rmtree(workspace, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
