# Public control.json Contract

> **Status**: current · **Updated**: 2026-08-26 · **Audience**: external validators
> **Scope**: how clients read `control.json`. Not how the owner publishes it.
> **Authority note**: it is not a backend configuration source — see
> `openroboto-backend/docs/adr/01-control-json不是后端配置源.md` ("control.json is
> not a backend config source").

`control.json` is a public, read-only document consumed by the weight-setting
validator. This repository documents how clients interpret the file. It does not
include tools or procedures for changing subnet state.

## 🔴 What this client still reads: `public_key`

**Miners read nothing from this file.** It was written when there was no
competitions table, so one static JSON had to carry a whole round's spec — one
round number, one dataset, one base checkpoint, one fee, one set of
hyperparameters, for a subnet that now runs several competitions at once. Every
one of those fields has a home of its own today:

| Was in control.json | Is now |
|---|---|
| `round` | `competition.seq` |
| `status` | `competition.status` (`draft` / `active` / `archived` — three words where this file had one) |
| `payment.burn_rate_tao` | `competition.params.fee.amount_tao`, confirmed against the backend in the second before it is paid |
| `dataset.train_url` / `val_url` | `competition.params.training.dataset` (`{train, val}`) |
| `training.vla_checkpoint_path` | `competition.params.training.checkpoint` |
| `training.vla_model_id` | nothing read it; which base model a season runs on is `competition.base_model_family` |
| `epochs`, `batch_size`, `learning_rate`, `lora_r`, `lora_alpha` | the `training:` section of `miner.yaml` — **the miner's**, not the subnet's |
| `process.*`, `payment.enabled`, `dataset.version`, and the other four LoRA knobs | nothing read them |

The competition section is written into `miner.yaml` by `openroboto init`, so
`build` / `train` / `check` stay offline.

| Field | Purpose |
|---|---|
| `public_key` | Credential for public read-only API access. **The only field this client still reads**, and the only channel an external validator has for it |

No write credential, private task payload, held-out mapping, wallet material, internal host, or owner command belongs in this document.

## Client behavior

The weight-setting validator fetches the URL configured as `urls.control_json`
(ETag-cached) and follows `public_key` when it rotates -- **that key is the whole
of what this client reads out of the file**. `openroboto doctor` also fetches it
as a reachability check.

🔴 **No payment path reads it any more** (2026-08-26). The payment path used to
fall back to `payment.burn_rate_tao` when `miner.yaml` had no competition
section; that rate is subnet-wide, the subnet runs several seasons at once, and
a fee paid without a season attached is filed under whichever season the backend
defaults to -- non-refundably. Such a workspace is now refused with an
instruction to re-run `openroboto init`.

🔴 **The URL must keep answering.** External validators run code we cannot make
them upgrade, and without that key they cannot read the weights they set.

Unknown fields should be ignored for forward compatibility. A client should stop safely when required public fields are missing or invalid.

## Example

The canonical placeholder sample is [`control_json_example.json`](./control_json_example.json),
reproduced here so the fields above and the shape below cannot drift apart.

⚠️ **The published file still carries all of the fields below, and nothing is
being taken out of it.** They simply have no reader left in this client except
`public_key` (and `payment` on the one path named above) — see the table
further up for where each one moved. Removing keys from a file external
validators parse buys nothing and risks breaking a client we cannot see.

```json
{
  "round": 1,
  "status": "active",
  "message": "Round 1 is active",
  "payment": {
    "enabled": true,
    "burn_rate_tao": 0.1,
    "limit_price_rao": 0
  },
  "dataset": {
    "version": "v1",
    "train_url": "https://example.com/public/train.json",
    "val_url": "https://example.com/public/val.json"
  },
  "training": {
    "vla_model_id": "pi05",
    "vla_checkpoint_path": "<public-checkpoint-path>",
    "epochs": 3,
    "batch_size": 4,
    "learning_rate": 0.0001,
    "warmup_ratio": 0.05,
    "gradient_accumulation_steps": 8,
    "lora_r": 32,
    "lora_alpha": 64,
    "lora_dropout": 0.1
  },
  "public_key": "<public-read-key>"
}
```

Every URL and credential above is a placeholder. The sample contains only
miner-visible and validator-visible fields.

> **The `payment` block above has no reader left in this client.** The fee is
> the entering season's `competition.params.fee.amount_tao`, confirmed against
> the backend in the second before it is paid. It is still published because
> other clients parse it, and taking keys out of a document we do not control the
> readers of buys nothing.

