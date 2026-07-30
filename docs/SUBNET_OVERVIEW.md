# OpenRoboto Subnet Protocol and Incentives

OpenRoboto is a Bittensor mainnet subnet for open robot-learning models. Miners fine-tune a public vision-language-action base model, publish an immutable Hugging Face revision, and announce it on netuid 80. Evaluation uses the public LIBERO toolkit and a reproducible post-submission seed.

## Public evidence

An independent reviewer can inspect:

- the miner hotkey and model commitment on chain;
- the pinned Hugging Face repository and commit;
- the evaluation-burn transaction;
- the commitment block hash, drand round, randomness, and derived seed;
- the public benchmark implementation, baseline, scoring rules, ranking, and final chain weights.

Held-out task data and the scoring-service deployment remain private.

## Submission lifecycle

```text
miner trains model
      |
      v
merge -> full checkpoint
      |
      v
immutable HF commit
      |
      v
evaluation burn + chain commitment
      |
      v
payment and format verification
      |
      v
public seed derivation + LIBERO evaluation
      |
      v
public ranking -> validator.py -> set_weights
```

The commitment binds the submitting hotkey, round, model repository and commit, payment reference, and block information. A malformed or unpaid submission does not enter evaluation.

## Submission artifact requirements

The evaluation service loads **complete model checkpoints only**. Every submission passes a structural pre-check (`libero_eval/check_model.py` in the [evaluation repository](https://github.com/openroboto-ai/openroboto-evaluation)) before any GPU time is spent; a submission that fails it is marked `failed` and the rejection reason is recorded so the miner can see exactly why.

| Requirement | Detail |
|---|---|
| Checkpoint format (one of) | openpi JAX: a `params/` directory (orbax OCDBT) · openpi PyTorch: a `model.safetensors` file |
| Normalization stats | `assets/physical-intelligence/libero/norm_stats.json` (state dim 8, action dim 7) |
| Architecture | Must match π0.5 (`pi05_libero` inference config); total parameter count within 2.5B–4.5B |
| **Not accepted** | **A bare LoRA adapter** (`adapter_config.json` + `adapter_model.safetensors`). The evaluator performs no merging — if you train with LoRA, merge the adapter back into the π0.5 base and export the full checkpoint before uploading. |

The pre-check is pure CPU and public — run it yourself before paying the submission fee:

```bash
# from the evaluation repository
uv run libero_eval/check_model.py --model /path/to/checkpoint --config pi05_libero
# exit 0 = will be accepted, exit 1 = would be rejected (reasons printed)
```

## Evaluation fee

The current fee is published as `payment.burn_rate_tao` in `control.json`. The miner burns the amount rather than transferring it to an operator. The payment reference is included in the chain commitment so reviewers can verify the transaction and its relationship to the submission.

## Seed and evaluation

The base seed is:

```text
uint32(SHA256("{commitment_block_hash}:{round_num}:{drand_randomness}")[-4:])
```

The formula and reference code are public in `protocol/seed.py`. Future entropy is unavailable before submission; recorded inputs make the result reproducible afterward. The public validator toolkit translates the base seed into per-task LIBERO initial states and records the inputs needed for replay.

The seed formula does not identify or select private held-out tasks. It randomizes public evaluation mechanics such as initial object placement.

## Ranking and weights

The public protocol constants define a champion challenge margin, Top-K limit, and emission distribution. A challenger must exceed the current champion by the published margin. `validator.py` reads the resulting public hotkey-to-weight map, normalizes positive weights, and calls Bittensor `set_weights`.

Review the active round metadata before assuming defaults from source code; changes must be public for the round in which they apply.

## Public interfaces

- `control.json` publishes round, status, fee, public training resources, training parameters, and an optional read credential.
- The read-only API publishes health, round summaries, rankings, miner summaries, benchmark metadata, exports, and weights.
- The public validator repository contains the benchmark worker, LIBERO evaluation implementation, tests, and initial-state generation.

Write endpoints, evaluation queues containing task payloads, owner controls, databases, and service credentials are outside this repository.

## Chain commitment shape

```json
{
  "s": "<miner-hotkey>",
  "h": "<commitment-block-hash>",
  "c": "<model-commit>",
  "r": 1,
  "i": "<namespace/model-repository>",
  "b": "<burn-transaction-hash>",
  "bb": 123456
}
```

Field names and encoding are implemented in `utils/chain.py`.

## Local verification

1. Confirm the chain commitment and pinned model commit.
2. Verify the burn transaction against the public round fee.
3. Recompute the seed with `protocol/seed.py` and the recorded drand value.
4. Run the public validator toolkit on the same model revision.
5. Compare the reproduced result, public ranking, and metagraph weights.

