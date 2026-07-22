# Evaluation Seed Derivation

## Public formula

OpenRoboto derives each submission's base evaluation seed from three public inputs:

1. the hash of the block containing the miner commitment;
2. the subnet round number;
3. randomness from a recorded drand quicknet round.

```text
message = UTF8("{block_hash}:{round_num}:{drand_randomness}")
digest  = SHA256(message)
seed    = big_endian_uint32(digest[-4:])
```

The reference implementation is `protocol/seed.py`:

```python
import hashlib


def derive_seed(block_hash: str, round_num: int, drand_random: str) -> int:
    seed_input = f"{block_hash}:{round_num}:{drand_random}".encode("utf-8")
    digest = hashlib.sha256(seed_input).digest()
    return int.from_bytes(digest[-4:], byteorder="big")
```

No private task, held-out data identifier, or dataset mapping participates in this formula.

## Why the formula is public

When a miner commits a model, the containing block hash is not yet fixed and the later drand value is not yet available. Publishing the formula therefore does not give a miner the future seed. After the public inputs are recorded, anyone can reproduce the seed exactly.

The two entropy sources are operationally independent. Reviewers should still verify the recorded block hash, drand round, and randomness rather than trusting a reported seed.

## Drand verification

The public drand quicknet chain identifier is defined in `protocol/seed.py`. For a recorded round, retrieve:

```text
https://api.drand.sh/<quicknet-chain-hash>/public/<drand-round>
```

Confirm that the returned `round` and `randomness` match the published evaluation record. Then call `derive_seed` with the commitment block hash and subnet round.

If drand is unavailable, evaluation must wait. The reference protocol does not fall back to block-hash-only derivation because that would change the published formula and reduce the independent entropy sources.

## Reproducible example

```python
from protocol.seed import derive_seed

block_hash = "0x" + "11" * 32
round_num = 1
drand_randomness = "22" * 32

assert derive_seed(block_hash, round_num, drand_randomness) == 3898936287
```

## From base seed to LIBERO initial states

The public validator toolkit derives a stable seed for each suite and task:

```text
task_seed = (base_seed * 1000003 + crc32("{suite}/{task_name}")) mod 2^32
```

`libero_eval/gen_init_states.py` then seeds NumPy and samples object positions and rotations from the public BDDL ranges. The mixed evaluation uses a documented split of official and seeded initial states. See the validator repository's `libero_eval/init_mix.py`, `libero_eval/gen_init_states.py`, tests, and `docs/init_state_randomization.md`.

This translation is deterministic and public. It randomizes the evaluation mechanics without disclosing any private task data.

## Audit checklist

- obtain the miner commitment and containing block hash from chain;
- obtain the recorded drand round and randomness from the public beacon;
- recompute the uint32 base seed with `protocol/seed.py`;
- recompute per-task seeds with the public validator toolkit;
- rerun the pinned model commit against the documented benchmark configuration.

