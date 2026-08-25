"""Run the four legacy commands with the chain and HuggingFace faked, and record
exactly what came out.

Two callers, one implementation, and that is the point:

- `scripts/gen_baseline.sh` runs this **inside a git worktree of an older
  commit** to produce `tests/fixtures/baseline/`;
- `tests/test_backward_compat.py` runs it against today's tree and compares.

If the two sides ran different harnesses, a difference between them would show
up as "backward compatibility broke" and cost an afternoon. So this module has
**no pytest import** and no dependency on anything the older commit does not
already have -- it must be copyable into that worktree and run with
`python tests/baseline_capture.py <outdir>`.

Everything that would otherwise vary between runs is nailed down: the block
hash, the burn tx, the HF commit and the wallet all come from the constants
below, so a payload recorded a week ago is comparable byte for byte with one
produced right now.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

FIXTURES = Path(__file__).resolve().parent / "fixtures"
LEGACY_CONFIG = FIXTURES / "miner_legacy.yaml"

#: The four commands AGENTS.md §1 says must not change behaviour.
COMMANDS = ("upload", "burn", "announce", "submit")

#: Only `announce` and `submit` reach `encode()`. `upload` pushes to HF and
#: `burn` sends `add_stake_burn`; neither builds a commitment payload, so a
#: "payload baseline" for them would be the hash of an empty string -- a
#: fixture that is green forever and guards nothing.
COMMANDS_WITH_PAYLOAD = ("announce", "submit")

ROUND = 1
HOTKEY = "5" + "M" * 47  # matches subnet.hotkey_ss58 in miner_legacy.yaml
REPO_ID = "legacyminer/pi05-MMMMMMMMMMMM"
HF_COMMIT = "a" * 40
HF_URL = f"https://huggingface.co/{REPO_ID}/commit/{HF_COMMIT}"
BURN_TX = "0x" + "d" * 64
BURN_BLOCK = 8_888_880
CURRENT_BLOCK = 8_888_888
BLOCK_HASH = "0x" + "c" * 64


@dataclass(frozen=True)
class Capture:
    """What one command produced. `payload_hex` is empty for commands that
    build no commitment."""

    exit_code: int
    stdout: str
    stderr: str
    payload_hex: str


class _FakeSubtensor:
    def get_current_block(self) -> int:
        return CURRENT_BLOCK

    def get_block_hash(self, block: int) -> str:
        return BLOCK_HASH

    def close(self) -> None:
        pass


class _FakeWallet:
    class hotkey:
        ss58_address = HOTKEY


def _seed_state(command: str) -> dict[str, Any]:
    """What the checkpoint holds when this command starts.

    Each command is captured from the state its own predecessor would have left
    behind, so they can be recorded independently and in any order.
    """
    uploaded = {
        "hf_repo_id": REPO_ID,
        "hf_url": HF_URL,
        "hf_commit": HF_COMMIT,
        "hotkey_ss58": HOTKEY,
        "step": "upload",
        "status": "completed",
    }
    if command in ("upload", "submit"):
        return {"hotkey_ss58": HOTKEY}
    if command == "burn":
        return uploaded
    return {**uploaded, "burn_tx_hash": BURN_TX, "burn_block": BURN_BLOCK}


@contextlib.contextmanager
def _faked_world(payloads: list[bytes]) -> Iterator[None]:
    """Chain, wallet and HuggingFace replaced by constants.

    Patched on the modules that own the names rather than on `submit` -- the
    pipeline command calls `perform_upload` / `perform_burn` /
    `perform_announce`, and those look their own dependencies up in their own
    module globals.
    """
    from openroboto_protocol.commitment import encode

    from openroboto.chain.commitment import SubmitResult
    from openroboto.commands import announce as announce_module
    from openroboto.commands import burn as burn_module
    from openroboto.commands import upload as upload_module
    from openroboto.huggingface import UploadResult
    from openroboto.payment import BurnReceipt

    def fake_submit(subtensor: Any, wallet: Any, netuid: int, payload: Any) -> Any:
        payloads.append(encode(payload))
        return SubmitResult(
            ok=True,
            extrinsic_hash="0x" + "e" * 64,
            block_height=CURRENT_BLOCK,
            extrinsic_index=2,
            fee_tao=0.0,
            confirmed=True,
        )

    patches: list[tuple[Any, str, Any]] = [
        (upload_module, "push_model", lambda **kwargs: UploadResult(HF_URL, HF_COMMIT)),
        (burn_module, "refresh_burn_rate", lambda *a, **k: None),
        (burn_module, "get_subtensor", lambda network: _FakeSubtensor()),
        (burn_module, "open_wallet", lambda settings: _FakeWallet()),
        (
            burn_module,
            "execute_stake_burn",
            lambda **kwargs: BurnReceipt(tx_hash=BURN_TX, block_number=BURN_BLOCK),
        ),
        (announce_module, "get_subtensor", lambda network: _FakeSubtensor()),
        (announce_module, "open_wallet", lambda settings: _FakeWallet()),
        (announce_module, "submit_announcement", fake_submit),
    ]
    saved = [(obj, name, getattr(obj, name)) for obj, name, _ in patches]
    for obj, name, value in patches:
        setattr(obj, name, value)
    try:
        yield
    finally:
        for obj, name, value in saved:
            setattr(obj, name, value)


def capture(command: str, workdir: Path) -> Capture:
    """Run one command in `workdir` and record its output, exit code and (if it
    has one) the exact bytes it would have written on chain."""
    import argparse
    import importlib

    module = importlib.import_module(f"openroboto.commands.{command}")

    workdir.mkdir(parents=True, exist_ok=True)
    shutil.copy(LEGACY_CONFIG, workdir / "miner.yaml")
    model_dir = workdir / "model"
    model_dir.mkdir(exist_ok=True)

    args = argparse.Namespace(
        config="miner.yaml",
        round=ROUND,
        output_dir=str(model_dir),
        force=False,
    )

    payloads: list[bytes] = []
    out, err = io.StringIO(), io.StringIO()
    previous = Path.cwd()
    os.chdir(workdir)
    try:
        from openroboto.round_state import save_state

        save_state(ROUND, _seed_state(command))
        with _faked_world(payloads):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                exit_code = int(module.run(args))
    finally:
        os.chdir(previous)

    if command in COMMANDS_WITH_PAYLOAD and not payloads:
        raise AssertionError(
            f"`{command}` sent no commitment -- a baseline recorded from this run "
            f"would be an empty payload that can never go red"
        )
    return Capture(
        exit_code=exit_code,
        stdout=out.getvalue(),
        stderr=err.getvalue(),
        payload_hex=payloads[0].hex() if payloads else "",
    )


def capture_all(workroot: Path) -> dict[str, Capture]:
    return {name: capture(name, workroot / name) for name in COMMANDS}


def write_baseline(destination: Path, captures: dict[str, Capture]) -> None:
    """Freeze the captures into the fixture layout `test_backward_compat.py`
    reads."""
    from importlib.metadata import version

    destination.mkdir(parents=True, exist_ok=True)
    for name, result in captures.items():
        (destination / f"stdout_{name}.txt").write_text(result.stdout, encoding="utf-8")
        if result.payload_hex:
            (destination / f"payload_{name}.hex").write_text(
                result.payload_hex + "\n", encoding="utf-8"
            )
    (destination / "exit_codes.json").write_text(
        json.dumps({n: c.exit_code for n, c in captures.items()}, indent=2) + "\n",
        encoding="utf-8",
    )
    (destination / "PROTOCOL_VERSION").write_text(
        version("openroboto-protocol") + "\n", encoding="utf-8"
    )


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python tests/baseline_capture.py <destination>", file=sys.stderr)
        return 2
    destination = Path(argv[1]).resolve()
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        captures = capture_all(Path(tmp))
    write_baseline(destination, captures)
    for name, result in captures.items():
        size = len(result.payload_hex) // 2
        print(f"{name}: exit={result.exit_code} payload={size}B")
    print(f"written to {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
