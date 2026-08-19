# OpenRoboto Public Architecture

## Scope

This repository contains the miner, public protocol, training runner, and the lightweight validator that submits weights. The separate public `validator` repository contains the LIBERO evaluation harness and baseline tooling.

Held-out evaluation inputs, the scoring-service deployment, databases, and subnet-owner operational tools are outside the public source boundary.

## Components

| Component | Where | Responsibility |
|---|---|---|
| CLI entry point | `src/openroboto/cli.py` | Command assembly; one module per command under `commands/` |
| Training | `commands/train.py`, `training/` | Read public round data, download training resources, run training in Docker |
| Submission | `commands/{upload,burn,announce,submit}.py` | Upload a model, pay the evaluation burn, announce on chain |
| Pre-flight | `commands/doctor.py`, `commands/check.py`, `preflight.py` | Everything checkable **before** money is spent |
| Chain access | `chain/`, `payment/` | Commitments and burn extrinsics |
| Hugging Face | `huggingface/` | Model upload and commit resolution |
| Config | `config/` | `miner.yaml` parsing plus the single `control.json` fetcher |
| Training runtime | `openpi-runner/` | Isolated OpenPI execution environment |
| Protocol | `openroboto-protocol` (installed package) | Commitment encoding, seed derivation, shared constants |
| External validator | `commands/validator.py` | Read public weights and call Bittensor `set_weights` |

The `protocol/` directory still present in this repository is a deprecated
leftover re-exporting the installed package; nothing imports it, and CI blocks
new copies. Shared types come from `openroboto-protocol`, never from a local copy
— the reason is a real incident where the two drifted and miners encoded what the
backend could not decode.

## Submission flow

```text
public control.json
        |
        v
openroboto train  -> OpenPI training (Docker) -> local model artifact
        |
        v
openroboto check  -> local format validation (still free)
        |
        v
openroboto submit -> Hugging Face commit -> evaluation burn -> chain commitment
```

The chain commitment binds the miner hotkey, model repository and commit, round number, payment reference, and commitment block information.

## Evaluation and seed flow

```text
commitment block hash + round number + drand randomness
                         |
                         v
                   protocol/seed.py
                         |
                         v
                   public uint32 seed
                         |
                         v
             public LIBERO evaluation toolkit
```

The formula is deterministic and public. The block hash and drand value become available after submission, which prevents pre-submission adaptation to a future seed. The seed randomizes public evaluation mechanics; it does not reveal held-out task data.

## Weight flow

`validator.py` performs only these actions:

1. fetch public `control.json`;
2. read weights from the documented read-only endpoint;
3. map miner hotkeys to UIDs and normalize positive weights;
4. submit weights through Bittensor `set_weights`.

It does not import a scoring service or owner module and does not accept a write credential.

## Local data boundary

Real YAML configuration, environment files, databases, runtime state, logs, and model weights are ignored by Git. Public examples use placeholders only.

