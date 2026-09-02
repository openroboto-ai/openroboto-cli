# OpenRoboto Public Architecture

> **Status**: current · **Updated**: 2026-09-02 · **Audience**: contributors to this CLI
> **Scope**: How the `openroboto` package is organised and where each responsibility lives.
> **Note**: Not a miner document. Miners want [MINER.md](./MINER.md).

## Scope

This repository contains the miner, public protocol, training runner, and the lightweight validator that submits weights. The separate public `validator` repository contains the LIBERO evaluation harness and baseline tooling.

Held-out evaluation inputs, the scoring-service deployment, databases, and subnet-owner operational tools are outside the public source boundary.

## Components

| Component | Where | Responsibility |
|---|---|---|
| CLI entry point | `src/openroboto/cli.py` | Command assembly; one module per command under `commands/` |
| Training | `commands/train.py`, `training/` | Read the season spec from `miner.yaml`, download training resources, run training in Docker |
| Submission | `commands/submit.py` | Upload a model, pay the entry fee, announce on chain. The three steps live in `commands/{upload,burn,announce}.py` as functions — they stopped being commands in 1.0, because running one alone is how a fee gets paid for a submission that is never announced |
| Pre-flight | `commands/doctor.py`, `commands/check.py`, `preflight.py` | Everything checkable **before** money is spent |
| Chain access | `chain/`, `payment/` | Commitments, and the burn / transfer extrinsics that pay the entry fee |
| Hugging Face | `huggingface/` | Model upload and commit resolution |
| Config | `config/` | `miner.yaml` parsing, the environment table, and the single `control.json` fetcher (validators only) |
| Training runtime | `src/openroboto/runner/` (π0.5) and `src/openroboto/runner/lingbot/` (LingBot-VLA 2.0) | One image definition per base model; `runner_context()` picks by `competition.base_model_family`. Both ship in the wheel so `openroboto build` needs no clone |
| Protocol | `openroboto-protocol` (installed package) | Commitment encoding, seed derivation, shared constants |
| External validator | `commands/validator.py` | Read public weights and call Bittensor `set_weights` |

There is no `protocol/` directory in this repository, and CI plus
`tests/test_vendored_protocol.py` both fail if one appears. Shared types come from
`openroboto-protocol`, never from a local copy — the reason is a real incident where
the two drifted and miners encoded what the backend could not decode.

## Submission flow

```text
GET /api/v1/competitions   (openroboto init, once)
        |
        v
miner.yaml `competition:`  -- the season's whole spec, read offline from here on
        |
        v
openroboto train  -> training in Docker -> local model artifact
        |
        v
openroboto check  -> local format validation (still free)
        |
        v
openroboto submit -> Hugging Face commit -> season re-checked against the backend
                     -> layout gate -> entry fee (burn or transfer) -> chain commitment
```

The chain commitment binds the miner hotkey, model repository and commit, season ordinal, competition id, payment reference, and commitment block information.

## Evaluation and seed flow

```text
commitment block hash + competition id + drand randomness
                         |
                         v
           openroboto_protocol.seed.derive_seed
                         |
                         v
                   public uint32 seed
                         |
                         v
             public LIBERO evaluation toolkit
```

🔴 The second input is the **competition id**, not the payload's `r` and not the
season ordinal an API response displays — see [SEED_GENERATION.md](./SEED_GENERATION.md), which
is the authority on this.

The formula is deterministic and public. The block hash and drand value become available after submission, which prevents pre-submission adaptation to a future seed. The seed randomizes public evaluation mechanics; it does not reveal held-out task data.

## Weight flow

`commands/validator.py` performs only these actions:

1. fetch public `control.json` — **for `public_key` and nothing else**;
2. read weights from the documented read-only endpoint (`/api/v1/weights`, falling
   back to the pre-v1 `/api/weights`);
3. map miner hotkeys to UIDs and normalize positive weights;
4. submit weights through Bittensor `set_weights`.

It does not import a scoring service or owner module and does not accept a write credential.

## Local data boundary

Real YAML configuration, environment files, databases, runtime state, logs, and model weights are ignored by Git. Public examples use placeholders only.
