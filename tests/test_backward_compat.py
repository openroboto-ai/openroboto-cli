"""The commitment encoding still behaves exactly as it did — to the byte.

This repository is installed by people who are not on our team. Break it and
nothing tells us; the only symptom is submissions dropping. **This file is that
"tells us".**

🔴 **`burn` and `submit` left this set on 2026-08-26, deliberately.** Paying used
to fall back to control.json's subnet-wide rate whenever `miner.yaml` had no
competition section, and that fee bought a place in whichever season the backend
defaults to -- non-refundably, with no error printed. Refusing outright is a
behaviour change on a path this file existed to freeze, so the freeze is lifted
here rather than worked around: their exit code went 0 → 1, and pinning it would
pin a bug. See `baseline_capture.COMMANDS`, `commands/burn.py` and ADR 05.

What survives that is the guarantee the file is actually for. `announce` is where
`encode()` is called; `submit` only ever reached those bytes through
`perform_announce`, so `payload_announce.hex` carries the claim below on its own.

What it pins is one sentence:

> A commitment encoded by `openroboto-protocol` **0.6.0** and one encoded by
> **0.7.0** are byte-for-byte identical, as long as the miner's `miner.yaml`
> has no competition section.

Both halves of that sentence are nailed down, in two different files, because
either half alone is a tautology:

- here: the baseline was recorded on 0.6.0
  (`tests/fixtures/baseline/PROTOCOL_VERSION`), and today's tree is compared
  against it;
- in `tests/test_packaging.py`: what is installed today is 0.7.0.

Regenerate the baseline in a 0.7.0 environment and this file starts comparing
0.7.0 with 0.7.0 — green forever, guarding nothing, with no error to say so.
`scripts/gen_baseline.sh` refuses to do it and the first test below catches it
if someone writes the files by hand.

Chain, wallet and HuggingFace are faked by `tests/baseline_capture.py`: no
network, no GPU, no wallet, no skips.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from openroboto_protocol.commitment import decode

from tests.baseline_capture import (
    COMMANDS,
    COMMANDS_WITH_PAYLOAD,
    Capture,
    capture_all,
)

BASELINE = Path(__file__).resolve().parent / "fixtures" / "baseline"
LEGACY_CONFIG = Path(__file__).resolve().parent / "fixtures" / "miner_legacy.yaml"

#: The key set of every commitment written before 0.7.0. `cid` and `m` are the
#: two keys 0.7.0 adds, and a legacy config must produce neither.
LEGACY_KEYS = {"s", "h", "c", "r", "i", "b", "bb"}

#: Everything the baseline is made of. Listed rather than globbed on purpose: a
#: glob that finds nothing parametrizes zero cases and pytest reports a green
#: run, which is the exact failure this file exists to prevent.
BASELINE_FILES = (
    "COMMIT",
    "PROTOCOL_VERSION",
    "exit_codes.json",
    *(f"payload_{name}.hex" for name in COMMANDS_WITH_PAYLOAD),
    *(f"stdout_{name}.txt" for name in COMMANDS),
)


@pytest.fixture(scope="module")
def today(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Capture]:
    """Run the pinned commands against the tree as it is right now."""
    return capture_all(tmp_path_factory.mktemp("legacy"))


def test_the_baseline_was_recorded_on_protocol_0_6_0() -> None:
    """🔴 When this goes red, do not touch the baseline. Go and find out why it
    was regenerated.

    Without this line the comparison below silently degenerates into "the
    version installed today against itself".
    """
    assert (BASELINE / "PROTOCOL_VERSION").read_text().strip() == "0.6.0"


@pytest.mark.parametrize("name", BASELINE_FILES)
def test_the_baseline_is_complete(name: str) -> None:
    """A missing fixture must be an error, never a quietly skipped comparison."""
    path = BASELINE / name
    assert path.is_file(), f"{name} is missing; run scripts/gen_baseline.sh"
    assert path.read_text(encoding="utf-8").strip(), f"{name} is empty"


@pytest.mark.parametrize("command", COMMANDS_WITH_PAYLOAD)
def test_payload_is_byte_identical_to_the_baseline(
    command: str, today: dict[str, Capture]
) -> None:
    """The comparison is on the **hex of the raw bytes**, not on a digest.

    A digest is just as exact and answers nothing on the day it goes red, and on
    that day the first question is always "which key got added". Hex diffs down
    to the six bytes of `,"m":""`.
    """
    expected = (BASELINE / f"payload_{command}.hex").read_text().strip()
    assert today[command].payload_hex == expected


@pytest.mark.parametrize("command", COMMANDS_WITH_PAYLOAD)
def test_payload_carries_none_of_the_new_keys(
    command: str, today: dict[str, Capture]
) -> None:
    """A legacy config writes the seven historical keys and nothing else.

    The byte counts are checked as well as the decoded key set: `"m":""` decodes
    to `model_hash=""`, which a "is the field set" style assertion would happily
    accept while the on-chain bytes had already grown by six.
    """
    raw = bytes.fromhex(today[command].payload_hex)
    assert raw.count(b'"cid"') == 0
    assert raw.count(b'"m"') == 0

    # The key set is read off the JSON itself, because *that* is what goes on
    # chain; the decode is kept as well, to prove the same bytes still round
    # trip through the module the backend reads them with.
    assert set(json.loads(raw)) == LEGACY_KEYS
    assert decode(raw).payload.competition_id is None


# 🔴 **`test_stdout_is_unchanged` and `test_exit_codes_are_unchanged` were
#    removed on 2026-08-28, with `upload` / `burn` / `announce` themselves.**
#
#    They pinned the strings in a miner's tutorial and the `$?` in a miner's
#    shell script. Those are real things to protect -- but for commands that
#    no longer exist, a shell script does not read the old exit code, it gets
#    "invalid choice". Freezing the output of something nobody can type does
#    not keep a promise; it keeps a fixture.
#
#    ⚠️ The break is real and is called out in `docs/MIGRATION.md`: a script
#    running `openroboto burn` must move to `openroboto submit`.
#
#    What this file still guarantees is the sentence at the top, and it is the
#    one that matters to a miner whose submission has to be decodable by the
#    backend: **the bytes on chain have not changed.** That claim lives in
#    `payload_announce.hex`, and `perform_announce` still produces them.


def test_a_legacy_config_needs_no_new_field() -> None:
    """The fixture config is the whole claim: a miner does not edit anything.

    A `competition` section appearing in it would mean the commands above
    were exercised on a config that had already been migrated. The check is on
    the parsed mapping, not on the file text — the file's own comments talk
    about competitions.
    """
    assert "competition" not in yaml.safe_load(
        LEGACY_CONFIG.read_text(encoding="utf-8")
    )


def test_the_legacy_payload_holds_none_and_not_an_empty_string() -> None:
    """🔴 `is None`, not "is falsy".

    `encode()` decides with `if payload.model_hash is not None`, so `""` — also
    falsy, and the obvious thing to reach for as "no value" — writes `"m":""`
    onto the chain and breaks byte compatibility on the spot. The protocol
    package keeps the two apart deliberately: `""` means the miner supplied a
    value that cannot be used, and `check_payload` rejects it.

    The protocol package has its own test that `encode()` did not drift. This
    one is the other half: that **this repo hands it `None`**, which no test
    over there can see.
    """
    from openroboto.chain import build_payload

    payload = build_payload(
        hotkey_ss58="5" + "M" * 47,
        block_hash="c" * 64,
        hf_commit="a" * 40,
        round_num=1,
        hf_repo_id="legacyminer/pi05-MMMMMMMMMMMM",
        burn_tx_hash="0x" + "d" * 64,
        burn_block=8_888_880,
    )
    assert payload.competition_id is None
    assert payload.model_hash is None
