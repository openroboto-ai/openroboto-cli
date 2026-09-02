# Documentation index

> **Status**: current · **Updated**: 2026-09-02
> **Audience**: miners and external validators — the people who `pip install openroboto`
> and are **not** on the team.
> **Language**: everything published in this repository — these documents, the code
> comments and the docstrings — is English, because the people who read them are not
> on the team (`AGENTS.md` §4). The untracked working notes (`AGENTS.md`, `CLAUDE.md`,
> `.trellis/`) are the exception and are Chinese.

Every document below answers one question and is the only place that answers it. If
you find the same rule stated in two files, that is a bug — report it.

## Start here

| I want to… | Read |
|---|---|
| understand what this subnet rewards and how | [SUBNET_OVERVIEW.md](./SUBNET_OVERVIEW.md) |
| get from nothing to a first submission on the **π0.5** competition | [MINER.md](./MINER.md) |
| do the same on the **LingBot-VLA 2.0** competition | [MINER_LINGBOT.md](./MINER_LINGBOT.md) |
| move off `python rt.py` / `python miner.py`, or move from the π0.5 base to LingBot | [MIGRATION.md](./MIGRATION.md) |
| set up a real machine (GPU, Docker, systemd) | [MINER_DEPLOY.md](./MINER_DEPLOY.md) |
| know what every `miner.yaml` field does | [CONFIG.md](./CONFIG.md) |
| know exactly what the evaluation fee costs me and when it is wasted | [PAYMENT.md](./PAYMENT.md) |
| verify my evaluation seed was not rigged | [SEED_GENERATION.md](./SEED_GENERATION.md) |
| run an external weight-setting validator | [VALIDATOR.md](./VALIDATOR.md) |
| write my own training logic | [custom-training.md](./custom-training.md) |
| see how the CLI itself is put together | [ARCHITECTURE.md](./ARCHITECTURE.md) |

## Before you spend anything

Entry fees are **not refundable** — burned or transferred. Two commands exist purely
so that a mistake costs you nothing:

```bash
openroboto doctor    # config, competition + entry fee, HF permissions, balance, Docker, GPU, image
openroboto check     # the evaluation format rules, applied locally
```

Run both before `openroboto submit`. The rules they enforce are documented in
[PAYMENT.md](./PAYMENT.md) and [SUBNET_OVERVIEW.md](./SUBNET_OVERVIEW.md).

## What lives elsewhere

| Subject | Owner | Why not here |
|---|---|---|
| Backend HTTP endpoint contracts | `openroboto-backend` → `docs/specs/` | This package is public and miner-facing; it calls seven read-only endpoints (listed in `SUBNET_OVERVIEW.md` §10) and has no business documenting authenticated worker/admin routes |
| Seed derivation *implementation* | `openroboto-protocol` → `src/openroboto_protocol/seed.py` | One implementation, shared by backend and miners. [SEED_GENERATION.md](./SEED_GENERATION.md) explains how to **verify** it; the package **is** it |
| Commitment encoding, shared constants | `openroboto-protocol` | Red line #1 in `AGENTS.md`: anything both sides must agree on lives in the package, never in a local copy |

## Known documentation gaps

Recorded here rather than left to be discovered by a miner losing money:

- **There is no `openroboto merge`, and there will not be one** (decided 2026-08-25).
  Exporting a full checkpoint is part of **training**, so it belongs in your training
  script, not in this CLI (merging needs the model libraries, which cannot be
  installed alongside `bittensor` in one interpreter). The bundled strategies leave
  the export step blank and say so, rather than writing an adapter nothing can use.
  Run `openroboto check` before paying — it catches a bare adapter and a checkpoint
  nested too deep, for free.
- **[MINER_LINGBOT.md](./MINER_LINGBOT.md) §5 (writing the training script) is
  deliberately thin.** The container and the runner ship, but this repository has no
  LingBot training configuration template yet, so the export step is described rather
  than handed to you — an invented procedure there costs a week of GPU time.
- **Where a document names a fee, a round or a dataset URL, the competition row
  wins.** Those are published per season on `GET /api/v1/competitions` and copied into
  your `miner.yaml` by `openroboto init`; `control.json` is only read by external
  validators, for `public_key`.
