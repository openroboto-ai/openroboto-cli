# Public Configuration Reference

> **Status**: current · **Updated**: 2026-08-26 · **Audience**: miners, external validators
> **Scope**: Every `miner.yaml` / `validator.yaml` field: meaning, unit, and what breaks if it is wrong.
> **Note**: Season-scoped values (fee, dataset, base checkpoint) are **not** here
> either — `openroboto init` copies them into the `competition:` section of this
> same file, from the backend. They no longer come from `control.json`; see
> [control_json.md](./control_json.md) for what is left in that file.

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
| `urls` | `control_json` | Where the subnet publishes `public_key`. Miners need nothing from it; external validators have no other way to get that key |
| `competition` | the whole section | Which competition this workspace mines. **Written by `openroboto init`, rewritten by `openroboto init --refresh`** — the season's own spec, kept on disk so `build` / `train` / `check` never go online. Empty = the π0.5 simulation competition, as before |
| `competition` | `track`, `seq` | The durable key. `id` is stored too, but it is local to one backend database and is re-resolved from `(track, seq)` before it is sent anywhere |
| `competition` | `adapter` | Decides the rules `openroboto check` judges your checkpoint by (π0.5 and LingBot-VLA 2.0 accept different layouts) and whether `openroboto train` has a container for this season |
| `competition` | `params` | The season's spec verbatim: `fee`, `training` (`image` / `dataset` / `checkpoint`), `strategy_template`, `format`. Read, never rewritten by us — a new season adding a key needs no new CLI |
| `competition` | `params.training.dataset` | `{train, val}` — what this season trains on. `null` means the season has not published one, and `openroboto train` refuses rather than reaching for another season's data |
| `competition` | `params.training.checkpoint` | Where this season's training **starts**. `null` = the training image uses the base it was built around. 🔴 Not `base_repo`: that is the **baseline** the leaderboard measures you against, and for π0.5 the two were different addresses |
| `model` | `vla_checkpoint_path` | A base checkpoint you already have locally. Empty is normal; when the season names one, the season wins |
| `huggingface` | `token`, `username` | Local Hugging Face upload credentials |
| root | `custom_train_script` | Optional miner-owned training strategy path |
| `training` | `epochs`, `batch_size`, `learning_rate`, `lora_r`, `lora_alpha` | **Yours to tune** — that is the competition. They reach the container as `EPOCHS` / `BATCH_SIZE` / `LR` / `LORA_R` / `LORA_ALPHA`, which your strategy script reads out of `cfg`. Defaults `3 / 4 / 1e-4 / 32 / 64` are the values the subnet used to hand everybody. ⚠️ Write the learning rate `1.0e-4`, not `1e-4` — YAML reads the second as text |
| root | `log_level`, `log_dir` | Local logging |

Nothing else in this file is fetched. `openroboto train` opens no URL beyond the
dataset the season names: the round is `competition.seq`, the status is
`competition.status`, and the five hyperparameters above are yours.

> **A workspace with a `competition` section does not read `control.json` for its
> fee.** That season's `params.fee` is the fee, and `openroboto submit` confirms it
> against the backend in the moment before paying — printing which season, how long
> it has left, how much and to whom, and asking. That check cannot be turned off, and
> `--force` does not skip it. A backend it cannot reach is a refusal, not a warning:
> without an answer there is no way to say who the money would be going to.

> **`payment.burn_rate_tao` has no effect, wherever you set it.** Not in
> `miner.yaml` and not in `control.json`: an amount says how much, never *which
> competition*, and a fee paid with no season attached is filed under whichever
> season the backend defaults to — with the TAO already gone. The fee comes from
> `competition.params.fee` and nowhere else, and a workspace with no `competition`
> section is refused rather than charged a subnet-wide rate. There is deliberately
> no built-in default fee.

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

