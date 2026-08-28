"""Run the legacy commands that still have a legacy path, with the chain and
HuggingFace faked, and record exactly what came out.

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

#: The commands AGENTS.md §1 says must not change behaviour **and that a config
#: with no competition section can still reach**.
#:
#: 🔴 `burn` and `submit` were here until 2026-08-26 and are not any more, which
#: is a promise being retired rather than a fixture being tidied. Paying used to
#: fall back to control.json's subnet-wide rate whenever `miner.yaml` had no
#: competition section; that fee bought a place in whichever season the backend
#: defaults to, non-refundably, and it is now refused outright (ADR 05 -- installs
#: from before the rebuild are out of support). Their recorded exit code was 0 and
#: is now 1, so keeping them here would pin a behaviour we deliberately changed.
#:
#: What the retirement does **not** cost is the encoding guarantee this whole
#: baseline exists for: `announce` is where `encode()` is actually called, and
#: `payload_announce.hex` pins it byte for byte on its own. `submit` only reached
#: those bytes by way of `perform_announce`.
#: 🔴 **Only `announce`, and only its payload bytes** (2026-08-28).
#:
#: `upload` / `burn` / `announce` stopped being commands in 1.0 -- each was one
#: step of `submit`, and running a step alone is how a fee gets paid for a
#: submission that is never announced. Their stdout and exit codes were pinned
#: here; pinning the output of a command nobody can type is not a guarantee,
#: it is a fixture that can only ever be wrong about something that no longer
#: exists.
#:
#: What survives is the one sentence this file is actually for: **the bytes
#: that go on chain have not changed.** `perform_announce` is where `encode()`
#: is called, and it is still called -- by `submit`, in the same order, from
#: the same state. So the capture goes through that function directly.
COMMANDS = ("announce",)
COMMANDS_WITH_PAYLOAD = ("announce",)

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
    if command == "upload":
        return {"hotkey_ss58": HOTKEY}
    return {**uploaded, "burn_tx_hash": BURN_TX, "burn_block": BURN_BLOCK}


@contextlib.contextmanager
def _faked_world(payloads: list[bytes]) -> Iterator[None]:
    """Chain, wallet and HuggingFace replaced by constants.

    Patched on the modules that own the names rather than on the command being
    run: `perform_upload` / `perform_announce` look their own dependencies up in
    their own module globals.
    """
    from openroboto_protocol.commitment import encode

    from openroboto.chain.commitment import SubmitResult
    from openroboto.commands import announce as announce_module
    from openroboto.commands import upload as upload_module
    from openroboto.huggingface import UploadResult

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
    from openroboto.commands.announce import perform_announce
    from openroboto.config import Settings

    workdir.mkdir(parents=True, exist_ok=True)
    shutil.copy(LEGACY_CONFIG, workdir / "miner.yaml")
    model_dir = workdir / "model"
    model_dir.mkdir(exist_ok=True)

    payloads: list[bytes] = []
    out, err = io.StringIO(), io.StringIO()
    previous = Path.cwd()
    os.chdir(workdir)
    try:
        from openroboto.round_state import save_state

        save_state(ROUND, _seed_state(command))
        with _faked_world(payloads):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                # `perform_announce` returns True on success; the baseline's
                # `exit_code` keeps the shape it always had (0 = fine).
                from openroboto.round_state import load_state

                ok = perform_announce(
                    Settings.load("miner.yaml"), ROUND, load_state(ROUND)
                )
                exit_code = 0 if ok else 1
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
