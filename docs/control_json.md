# Public control.json Contract

> **Status**: current · **Updated**: 2026-08-19 · **Audience**: miners, external validators
> **Scope**: how clients read `control.json`. Not how the owner publishes it.
> **Authority note**: `control.json` carries **only** `payment` / `dataset` / `training` /
> `process`. It is not a backend configuration source — see
> `openroboto-backend/docs/adr/01-control-json不是后端配置源.md`.

`control.json` is a public, read-only contract consumed by miners and the weight-setting validator. This repository documents how clients interpret the file. It does not include tools or procedures for changing subnet state.

## Miner-visible fields

| Field | Purpose |
|---|---|
| `round` | Current round number |
| `status` | Public lifecycle state such as `active` or `paused` |
| `message` | Human-readable public notice |
| `payment.enabled` | Whether the evaluation fee is active |
| `payment.burn_rate_tao` | Evaluation fee announced for the round. `0` announces a free period: burn verification is skipped and submissions are accepted without payment |
| `payment.limit_price_rao` | Optional transaction price limit |
| `dataset.version` | Public training-data version |
| `dataset.train_url` | Public training-data URL |
| `dataset.val_url` | Public validation-data URL |
| `training.*` | Public base-model and training parameters |
| `public_key` | Optional credential for public read-only API access |

No write credential, private task payload, held-out mapping, wallet material, internal host, or owner command belongs in this document.

## Client behavior

Miners fetch the URL configured as `urls.control_json`, use ETag caching when available, and apply the announced round, payment, dataset, and training values. The weight-setting validator reads the same public file to refresh its read credential.

Unknown fields should be ignored for forward compatibility. A client should stop safely when required public fields are missing or invalid.

## Example

The canonical placeholder sample is [`control_json_example.json`](./control_json_example.json),
reproduced here so the fields above and the shape below cannot drift apart:

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

> **The fee is 0.1 TAO on mainnet today, but never hard-code it.** Read
> `payment.burn_rate_tao` from the live file. `openroboto burn` refuses to run when
> it cannot fetch this file rather than falling back to a guess — a wrong amount is
> rejected by the backend and the TAO is not refunded.

