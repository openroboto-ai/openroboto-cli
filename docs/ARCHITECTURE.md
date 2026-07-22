# OpenRoboto Public Architecture

## Scope

This repository contains the miner, public protocol, training runner, and the lightweight validator that submits weights. The separate public `validator` repository contains the LIBERO evaluation harness and baseline tooling.

Held-out evaluation inputs, the scoring-service deployment, databases, and subnet-owner operational tools are outside the public source boundary.

## Components

| Component | Files | Responsibility |
|---|---|---|
| Miner entry point | `miner.py` | Read public round data, download training resources, and run training |
| Submission CLI | `rt.py`, `payment.py` | Upload a model, pay the evaluation burn, and announce the model on chain |
| Miner modules | `miner/` | Training orchestration and Hugging Face publishing |
| Training runtime | `openpi-runner/` | Isolated OpenPI execution environment |
| Protocol | `protocol/` | Public data types and seed derivation |
| Weight validator | `validator.py` | Read public weights and call Bittensor `set_weights` |
| Shared utilities | `utils/` | Configuration, chain access, HTTP downloads, and logging |

## Submission flow

```text
public control.json
        |
        v
miner.py -> OpenPI training -> local model artifact
        |
        v
rt.py -> Hugging Face commit -> evaluation burn -> chain commitment
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

