#!/usr/bin/env python3
"""End-to-end run of the miner flow against Bittensor testnet and a live backend.

Covers steps 1-5 of the release gate (`openroboto-backend`'s
`docs/上线验收计划.md` S1): check -> upload -> burn -> announce -> the chain
scanner admits it. Steps 6-10 need a GPU worker to actually evaluate the model,
so they are out of scope here.

**Deliberately not collected by pytest.** `AGENTS.md` §4 says any skip in CI is
red, on the grounds that the unit suite needs no GPU, chain or network. This
script needs all but the GPU, so it is not a unit test -- it is a release gate.
Run it on a schedule or before a release, not on every pull request: a flaky
chain RPC turning someone's PR red teaches people to ignore red.

Every step drives the real `openroboto` entry point through a subprocess rather
than importing it, so exit codes are covered too -- miners chain these commands
as `check && submit`, and an exit code that disagrees with the printed verdict
is what sends them to `burn` with a checkpoint the backend will reject.

## Configuration (all via environment)

    E2E_HF_TOKEN          HuggingFace token with write + create-repo
    E2E_HF_USERNAME       HuggingFace account name
    E2E_BACKEND_URL       backend base URL, e.g. http://127.0.0.1:8000
    E2E_CONTROL_JSON      control.json URL the miner should read
    E2E_NETUID            defaults to 313
    E2E_NETWORK           defaults to "test"
    E2E_WALLET_NAME       local wallet name (developer machines)
    E2E_COLDKEY_MNEMONIC  coldkey mnemonic (CI; takes precedence)
    E2E_HOTKEY_MNEMONIC   hotkey mnemonic (CI; required with the above)
    E2E_KEEP_HF_REPO      set to 1 to keep the uploaded repo for inspection

The two mnemonics exist so CI can rebuild the wallet from secrets. They are
written to a temporary wallet directory that is removed on exit.

## What it costs

One burn per run at the round's rate (0.01 TAO on the dev control.json), plus
transaction fees. Testnet TAO, but not free -- a loop that reruns this on every
push will drain the wallet and then fail on an empty balance, which reads like a
code failure and is not one.
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


def write_config(workspace: Path, hotkey_ss58: str, wallet_name: str) -> Path:
    """Generate miner.yaml from the environment.

    `environment: dev` -- the preset for testnet 313 against
    `api-dev.openroboto.ai`, which is exactly what this script drives.

    It used to write `environment: local`, on the reasoning that local is the
    only preset that pins no network. That was true and still failed, because
    `local` also refuses to point at a hosted host: "environment=local, yet
    backend.url points at a hosted environment" is a contradiction, and
    `check_coherent()` said so before spending anything (2026-08-21, the first
    real run of this workflow -- it failed at step 3 of 5 with the HF repo
    already uploaded, and cleaned that repo up on the way out).

    That refusal was correct. The fix is to name the environment we are actually
    in, not to weaken the check.
    """
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
        "urls:\n"
        f'  control_json: "{env("E2E_CONTROL_JSON")}"\n'
        "huggingface:\n"
        f'  token: "{env("E2E_HF_TOKEN")}"\n'
        f'  username: "{env("E2E_HF_USERNAME")}"\n'
        "log_level: INFO\n",
        encoding="utf-8",
    )
    return config


def poll_for_submission(backend: str, hotkey: str, burn_tx: str) -> dict[str, object]:
    """Wait for the chain scanner to admit the submission we just announced.

    Matches on the burn transaction hash rather than "the newest row": a wallet
    is reused across runs, so it already carries older rows, and taking the
    newest one would let a scanner that never ran pass the step by reporting the
    previous run's success. The burn hash is unique per run and is the one value
    that appears both in the local round state and on the scanner's row.
    """
    from openroboto.backend_api import fetch_submissions

    deadline = time.monotonic() + SCAN_TIMEOUT_SEC
    seen: list[object] = []
    while time.monotonic() < deadline:
        envelope = fetch_submissions(backend, hotkey=hotkey, limit=20)
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
        config = write_config(workspace, hotkey, wallet_name)
        print(f"workspace: {workspace}\nhotkey:    {hotkey}\nbackend:   {backend}")

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

        step(2, "upload -- push the checkpoint to HuggingFace")
        result = run_cli(
            [
                "upload",
                "--config",
                str(config),
                "--round",
                "1",
                "--output-dir",
                str(checkpoint),
            ],
            workspace,
        )
        if result.returncode != 0:
            raise E2EError("`upload` failed; see the output above")
        repo_id = f"{env('E2E_HF_USERNAME')}/pi05-{hotkey[-12:]}"

        step(3, "burn -- pay the round's evaluation fee on chain")
        result = run_cli(["burn", "--config", str(config), "--round", "1"], workspace)
        if result.returncode != 0:
            raise E2EError("`burn` failed; see the output above")

        # announce must follow burn inside the backend's 50-block window, so do
        # not put anything slow between these two steps.
        step(4, "announce -- commit the submission on chain")
        result = run_cli(
            ["announce", "--config", str(config), "--round", "1"], workspace
        )
        if result.returncode != 0:
            raise E2EError("`announce` failed; see the output above")

        step(5, "scan -- the backend admits the submission")
        state = json.loads(
            (workspace / "state/round_1.json").read_text(encoding="utf-8")
        )
        burn_tx = str(state.get("burn_tx_hash") or "")
        if not burn_tx:
            raise E2EError(
                "the local round state carries no burn_tx_hash, so there is "
                "nothing to match the scanner's row against"
            )
        record = poll_for_submission(backend, hotkey, burn_tx)
        print(f"   scanner row: {json.dumps(record, default=str)[:400]}")

        status = str(record.get("eval_status", ""))
        if status == "rejected":
            raise E2EError(
                f"the submission was admitted but rejected: "
                f"{record.get('reject_reason') or '(no reason given)'}\n"
                "  → if this is BURN_INSUFFICIENT, the backend's BURN_RATE_TAO "
                "and the rate published in control.json disagree. The backend "
                "does not expose its expected rate, so a miner can only find "
                "this out by burning; keep the two in sync by hand."
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

        print("\n✅ steps 1-5 passed")
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
