# Public Configuration Reference

> **Status**: current · **Updated**: 2026-08-19 · **Audience**: miners, external validators
> **Scope**: Every `miner.yaml` / `validator.yaml` field: meaning, unit, and what breaks if it is wrong.
> **Note**: Round-scoped values (fee, dataset) are **not** here — they come from [control_json.md](./control_json.md).

Real configuration files are local-only. Copy an example, fill its placeholders, and keep the resulting YAML outside Git.

## Miner

`openroboto init` writes a filled-in template with every field commented:

```bash
openroboto init my-miner    # writes miner.yaml + train_strategy.py
```

> The template ships **inside the package**, so it cannot drift from the parser that
> reads it. There is no `miner.example.yaml` in the repository any more — there used
> to be one, and it had already diverged from the packaged template.

| Section | Field | Purpose |
|---|---|---|
| `subnet` | `network`, `netuid` | Bittensor network and subnet |
| `subnet` | `wallet_path`, `coldkey`, `hotkey`, `hotkey_ss58` | Local wallet selection |
| `subnet` | `wallet_password` | Optional local unlock value; never commit it |
| `urls` | `control_json` | Public miner-readable control document |
| `urls` | `dataset_train`, `dataset_val` | Public training and validation resources |
| `model` | `vla_model_id`, `vla_checkpoint_path`, `cache_dir` | Base model selection and local cache |
| `huggingface` | `token`, `username` | Local Hugging Face upload credentials |
| root | `custom_train_script` | Optional miner-owned training strategy path |
| root | `log_level`, `log_dir` | Local logging |

`payment` and selected training values are read from the public `control.json`.

> **Do not set `payment.burn_rate_tao` locally.** The announced fee is the only
> correct value; a stale local override burns the wrong amount, and a wrong amount is
> rejected with no refund. If `control.json` cannot be fetched, `openroboto burn`
> **refuses to run** rather than falling back to a guess — there is deliberately no
> built-in default fee.

## Weight-setting validator

```bash
openroboto init --validator    # writes validator.yaml, no strategy script
```

| Section | Field | Purpose |
|---|---|---|
| `subnet` | `network`, `netuid` | Bittensor network and subnet |
| `subnet` | wallet fields | Local validator wallet selection |
| `urls` | `control_json` | Public control document |
| `backend` | `url` | Read-only result service base URL |
| `backend` | `public_key` | Optional public read credential |
| root | `weight_interval_min` | Weight submission interval |

The weight-setting validator does not accept a scoring or management credential.

## Secret handling

- Never commit `miner.yaml`, `validator.yaml`, `.env`, wallet files, tokens, or passwords.
- Keep example values empty or use explicit placeholders.
- Prefer an interactive wallet prompt when practical.
- Review `git status` and run the repository sensitive scan before publishing.

