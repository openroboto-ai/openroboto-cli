# Public control.json Contract

`control.json` is a public, read-only contract consumed by miners and the weight-setting validator. This repository documents how clients interpret the file. It does not include tools or procedures for changing subnet state.

## Miner-visible fields

| Field | Purpose |
|---|---|
| `round` | Current round number |
| `status` | Public lifecycle state such as `active` or `paused` |
| `message` | Human-readable public notice |
| `payment.enabled` | Whether the evaluation fee is active |
| `payment.burn_rate_tao` | Evaluation fee announced for the round |
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

See `control_json_example.json` and `CONTROL_JSON_SAMPLE.md`. All URLs and credentials in those examples are placeholders.

