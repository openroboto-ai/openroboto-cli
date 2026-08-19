# Documentation index

> **Status**: current · **Updated**: 2026-08-19
> **Audience**: miners and external validators — the people who `pip install openroboto`
> and are **not** on the team.
> **Language**: these documents are English because the people who read them are not.
> Internal documents in this repository (`AGENTS.md`, `SCOPE.md`, code comments) are
> Chinese, per `AGENTS.md` §4.

Every document below answers one question and is the only place that answers it. If
you find the same rule stated in two files, that is a bug — report it.

## Start here

| I want to… | Read |
|---|---|
| understand what this subnet rewards and how | [SUBNET_OVERVIEW.md](./SUBNET_OVERVIEW.md) |
| get from nothing to a first submission | [MINER.md](./MINER.md) |
| set up a real machine (GPU, Docker, systemd) | [MINER_DEPLOY.md](./MINER_DEPLOY.md) |
| know what every `miner.yaml` field does | [CONFIG.md](./CONFIG.md) |
| know exactly what the evaluation fee costs me and when it is wasted | [PAYMENT.md](./PAYMENT.md) |
| read the round contract the subnet publishes | [control_json.md](./control_json.md) |
| verify my evaluation seed was not rigged | [SEED_GENERATION.md](./SEED_GENERATION.md) |
| run an external weight-setting validator | [VALIDATOR.md](./VALIDATOR.md) |
| see how the CLI itself is put together | [ARCHITECTURE.md](./ARCHITECTURE.md) |

## Before you spend anything

Burns are **not refundable**. Two commands exist purely so that a mistake costs you
nothing:

```bash
openroboto doctor    # GPU, Docker, HF permissions, balance, config, control.json
openroboto check     # the evaluation format rules, applied locally
```

Run both before `openroboto submit`. The rules they enforce are documented in
[PAYMENT.md](./PAYMENT.md) and [SUBNET_OVERVIEW.md](./SUBNET_OVERVIEW.md).

## What lives elsewhere

| Subject | Owner | Why not here |
|---|---|---|
| Backend HTTP endpoint contracts | `openroboto-backend` → `docs/specs/` | This package is public and miner-facing; it calls exactly four read-only endpoints (listed in `SUBNET_OVERVIEW.md` §10) and has no business documenting authenticated worker/admin routes |
| Seed derivation *implementation* | `openroboto-protocol` → `src/openroboto_protocol/seed.py` | One implementation, shared by backend and miners. [SEED_GENERATION.md](./SEED_GENERATION.md) explains how to **verify** it; the package **is** it |
| Commitment encoding, shared constants | `openroboto-protocol` | Red line #1 in `AGENTS.md`: anything both sides must agree on lives in the package, never in a local copy |

## Archive

[`archive/`](./archive/) holds superseded documents. They are kept rather than deleted
(`SCOPE.md`: inherited files are not deleted) and each carries a header naming what
replaced it. **Do not treat anything in `archive/` as current.**

| Archived | Replaced by |
|---|---|
| `archive/ROLES_BREAKDOWN.md` | [SUBNET_OVERVIEW.md](./SUBNET_OVERVIEW.md) §2 |
| `archive/CONTROL_JSON_SAMPLE.md` | [control_json.md](./control_json.md) |
| `api_reference_en.md` (moved out) | `openroboto-backend` → `docs/specs/`, original kept at that repo's `docs/archive/` |

## Known documentation gaps

Recorded here rather than left to be discovered by a miner losing money:

- **`openroboto merge` does not exist.** Training produces a LoRA adapter, and a bare
  adapter is rejected. Merging into a full checkpoint is currently manual. Run
  `openroboto check` before paying — it catches exactly this.
- **These guides still describe the subnet as of 2026-08-19.** The fee, round number
  and dataset URLs are published live in `control.json`; where a document names a
  number, the live file wins.
