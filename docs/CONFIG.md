# Public Configuration Reference

> **Status**: current · **Updated**: 2026-08-19 · **Audience**: miners, external validators
> **Scope**: Every `miner.yaml` / `validator.yaml` field: meaning, unit, and what breaks if it is wrong.
> **Note**: Round-scoped values (fee, dataset) are **not** here — they come from [control_json.md](./control_json.md).

Real configuration files are local-only. Copy an example, fill its placeholders, and keep the resulting YAML outside Git.

## `environment` — one name for four coupled settings

`subnet.network`, `subnet.netuid`, `urls.control_json` and `backend.url` all
describe the same decision: which subnet you are on, and which backend watches it.
They used to be four independent switches, and changing only some of them is not a
harmless mistake — both half-states cost money and neither announces itself:

- **control.json from dev, netuid still 80.** The dev backend publishes
  `burn_rate_tao: 0.01` while production publishes `0.1`. The burn goes to
  *mainnet* at a tenth of the required fee, production rejects it on the amount,
  and burns are not refunded.
- **netuid 313, backend still production.** The submission goes to testnet while
  `openroboto status` asks production about it. Nothing is ever found, and no error
  anywhere explains why.

So set one field:

| `environment` | Chain | Subnet | Backend |
|---|---|---|---|
| `mainnet` (default) | finney | 80 | `api.openroboto.ai` |
| `dev` | testnet | 313 | `api-dev.openroboto.ai` |
| `local` | *you decide* | *you decide* | *you must say where it is* |

```yaml
# Your own backend — for development, staging, or a colleague's machine
environment: local
subnet:   { netuid: 313, network: test }
backend:  { url: "http://localhost:8001" }
urls:     { control_json: "http://localhost:8001/control.json" }
```

Anything below the environment can still be set individually; the preset only
supplies defaults. What the CLI will not do is let the pieces disagree —
`openroboto doctor` reports a mismatch, and `burn` / `announce` / `validator run`
refuse to touch the chain:

```
❌ environment: environment=mainnet means netuid 80, but the config says 313.
     The two have to be changed together -- TAO burned on the wrong subnet is not refunded.
```

Two things it deliberately does **not** do:

- **It never supplies `subnet.netuid`.** That field has no default on purpose: a
  config that forgets it must fail rather than quietly pick a subnet, because
  picking the wrong one burns real TAO. The environment verifies the netuid you
  set; it does not choose one for you.
- **`local` clears the built-in URLs instead of inheriting them.** Those defaults
  point at production, so `environment: local` with the URLs left unset would
  quietly talk to mainnet's backend while you believed you were testing locally.
  `local` refuses to run until you say where your backend is.

> ⚠️ As of 2026-08-19 the deployed dev backend is still configured for mainnet
> (`netuid: 80`) — it is a sandbox in name only. `environment: dev` describes where
> dev is going, and will correctly refuse to pair with a mainnet netuid until the
> rebuilt backend is deployed there pointed at 313.

## Miner

`openroboto init` writes a filled-in template with every field commented:

```bash
openroboto init my-miner    # miner.yaml + train_strategy.py + README.md + .gitignore
```

> The template ships **inside the package**, so it cannot drift from the parser that
> reads it. There is no `miner.example.yaml` in the repository any more — there used
> to be one, and it had already diverged from the packaged template.

| Section | Field | Purpose |
|---|---|---|
| root | `environment` | `mainnet` \| `dev` \| `local` — see above. Sets the rest's defaults and refuses incoherent combinations |
| `subnet` | `network`, `netuid` | Bittensor network and subnet |
| `subnet` | `wallet_path`, `coldkey`, `hotkey`, `hotkey_ss58` | Local wallet selection |
| `subnet` | `wallet_password` | Optional local unlock value; never commit it |
| `urls` | `control_json` | Public miner-readable control document |
| `urls` | `dataset_train`, `dataset_val` | Public training and validation resources |
| `model` | `vla_model_id`, `vla_checkpoint_path` | Base model selection. Leave the path empty and the container downloads the base checkpoint into `./cache/pi05_base` |
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
| root | `environment` | Same as for miners |
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

