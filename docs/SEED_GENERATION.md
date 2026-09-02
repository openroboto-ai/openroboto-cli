# Evaluation Seed Derivation

> **Status**: current · **Updated**: 2026-08-19 · **Audience**: miners, auditors
> **Scope**: How the evaluation seed is derived and how to recompute it yourself.
> **Note**: The authoritative implementation is `openroboto_protocol.seed`, shared byte-for-byte with the backend. This document explains how to **verify** it.

## Public formula

OpenRoboto derives each submission's base evaluation seed from three public inputs:

1. the hash of the block containing the miner commitment;
2. **the competition id** — the `id` of the season the submission was admitted to.
   See the warning below: this is *not* the `r` you wrote into the commitment
   payload, and *not* the "round number" the API shows you;
3. randomness from a recorded drand quicknet round.

```text
message = UTF8("{block_hash}:{competition_id}:{drand_randomness}")
digest  = SHA256(message)
seed    = big_endian_uint32(digest[-4:])
```

The reference implementation is `openroboto_protocol.seed`
(`pip install "openroboto-protocol==0.9.0"` — the release this CLI pins):

```python
import hashlib


def derive_seed(block_hash: str, round_num: int, drand_random: str) -> int:
    # ⚠️ The parameter is still spelled `round_num` for compatibility, but what
    #    the backend passes is the **competition id** — see the warning below.
    #    The formula concatenates by position and never reads the name.
    seed_input = f"{block_hash}:{round_num}:{drand_random}".encode("utf-8")
    digest = hashlib.sha256(seed_input).digest()
    return int.from_bytes(digest[-4:], byteorder="big")
```

No private task, held-out data identifier, or dataset mapping participates in this formula.

### ⚠️ The second input is the competition id — not `r`, not the displayed round

🔴 **This section said the opposite until 2026-08-28.** It told you to take the
raw `r` from your own commitment payload. That was true once and is not any
more; following it now produces a seed that does not match.

The second input is `competitions.id` — the identity of the season the backend
admitted your submission to. Two things follow:

- **It is not `r`.** `r` is a number you write on your own machine, and in a
  payload carrying `cid` nothing validates it at all. A value a miner chooses
  cannot be an input to the seed that decides how that miner is evaluated.
- **It is not the number the API displays as the round.** That one is the
  season's `seq` — the human-facing "nth season" — and it restarts per track,
  so `(sim,1)` and `(real,1)` both show 1 while being different seasons.

Where to read the right value: the `competition_id` on your submission
(`GET /api/v1/submissions/{task_id}`), or `id` on the season itself
(`GET /api/v1/competitions`). Both are the same number that went into the hash.

⚠️ For every submission made to the first simulation season the three numbers
happen to coincide (`id` = `seq` = `r` = 1), so a check that used the wrong one
still passed. That is a coincidence of the first season, not a rule.

If a recomputed seed does not match, check this first.

## Why the formula is public

When a miner commits a model, the containing block hash is not yet fixed and the later drand value is not yet available. Publishing the formula therefore does not give a miner the future seed. After the public inputs are recorded, anyone can reproduce the seed exactly.

The two entropy sources are operationally independent. Reviewers should still verify the recorded block hash, drand round, and randomness rather than trusting a reported seed.

## Drand verification

The public drand quicknet chain identifier is `openroboto_protocol.seed.DRAND_CHAIN_HASH`. For a recorded round, retrieve:

```text
https://api.drand.sh/<quicknet-chain-hash>/public/<drand-round>
```

Confirm that the returned `round` and `randomness` match the published evaluation record. Then call `derive_seed` with the commitment block hash and the **competition id** (see the warning above).

If drand is unavailable, evaluation must wait. The reference protocol does not fall back to block-hash-only derivation because that would change the published formula and reduce the independent entropy sources.

A submission whose seed cannot be computed yet is reported with the non-terminal status `seed_failed` and is retried automatically on every scan cycle. A drand outage is an infrastructure condition, not a submission fault: no submission is rejected because of it, and no miner action is needed.

## Reproducible example

```python
from openroboto_protocol.seed import derive_seed

block_hash = "0x" + "11" * 32
competition_id = 1        # the parameter is still spelled `round_num`
drand_randomness = "22" * 32

assert derive_seed(block_hash, competition_id, drand_randomness) == 3898936287
```

⚠️ `tests/test_vendored_protocol.py::test_documented_seed_example_still_reproduces`
holds the same three inputs and the same result. Change one of them here and the
other has to move with it.

## From base seed to LIBERO initial states

The public validator toolkit derives a stable seed for each suite and task:

```text
task_seed = (base_seed * 1000003 + crc32("{suite}/{task_name}")) mod 2^32
```

`libero_eval/gen_init_states.py` then seeds NumPy and samples object positions and rotations from the public BDDL ranges. The mixed evaluation uses a documented split of official and seeded initial states. See the validator repository's `libero_eval/init_mix.py`, `libero_eval/gen_init_states.py`, tests, and `docs/init_state_randomization.md`.

This translation is deterministic and public. It randomizes the evaluation mechanics without disclosing any private task data.

## Audit checklist

- obtain the miner commitment and containing block hash from chain, then the
  **competition id** the submission was admitted to (`competition_id` on
  `GET /api/v1/submissions/{task_id}`, or `id` on `GET /api/v1/competitions`) —
  🔴 **not** the payload's `r`, see the warning above;
- obtain the recorded drand round and randomness from the public beacon;
- recompute the uint32 base seed with `openroboto_protocol.seed.derive_seed`;
- recompute per-task seeds with the public validator toolkit;
- rerun the pinned model commit against the documented benchmark configuration.
