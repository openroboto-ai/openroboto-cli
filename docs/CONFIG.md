# Public Configuration Reference

> **Status**: current · **Updated**: 2026-09-02 · **Audience**: miners, external validators
> **Scope**: Every `miner.yaml` / `validator.yaml` field: meaning, unit, and what breaks if it is wrong.
> **Note**: Season-scoped values (fee, dataset, base checkpoint) are **not** here
> either — `openroboto init` copies them into the `competition:` section of this
> same file, from the backend. For the one field validators read, see
> [VALIDATOR.md](./VALIDATOR.md) for the one field validators still read.

Real configuration files are local-only. Copy an example, fill its placeholders, and keep the resulting YAML outside Git.

## `environment` — one name for three coupled settings

`subnet.network`, `subnet.netuid` and `backend.url` all describe the same
decision: which subnet you are on, and which backend watches it. They are three
independent switches, and changing only some of them costs money without
announcing itself:

- **netuid 313, backend still production.** The submission goes to testnet while
  `openroboto status` asks production about it. Nothing is ever found, and no error
  anywhere explains why.
- **A season from one backend, a chain from another.** Every field agrees with
  every other field, and the fee still leaves on the subnet *this* file names.
  That is what `competition.source` exists to catch.

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
```

Anything below the environment can still be set individually; the preset only
supplies defaults. What the CLI will not do is let the pieces disagree —
`openroboto doctor` reports a mismatch, and `submit` / `validator run` refuse to
touch the chain:

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
| `competition` | the whole section | Which competition this workspace mines. **Written by `openroboto init`, rewritten by `openroboto init --refresh`** — the season's own spec, kept on disk so `build` / `train` / `check` never go online. 🔴 Not optional: `openroboto submit` refuses a workspace without it, before uploading anything, because a fee paid with no season attached is filed under whichever season the backend defaults to |
| `competition` | `track`, `seq` | The durable key. `id` is stored too, but it is local to one backend database and is re-resolved from `(track, seq)` before it is sent anywhere |
| `competition` | `adapter` | `sim_openpi` \| `sim_lingbot` \| `real_xarm6`. Decides only whether **this package ships a container** `openroboto train` can run for the season. 🔴 It does **not** say which base model: `real_xarm6` names a robot arm |
| `competition` | `base_model_family` | `pi0.5` \| `lingbot-vla-2.0`. Decides the rules `openroboto check` and the pre-payment layout gate judge your checkpoint by, which runner image `openroboto build` builds, and the default HF repository name. 🔴 **Without it `submit` refuses** rather than guessing — `openroboto init --refresh` writes it |
| `competition` | `source` | The backend that served this season, written by `init`. It is what catches "the season came from one backend and the fee leaves on another one's chain", which the five self-describing fields above cannot see because they agree with each other |
| `competition` | `base_repo`, `base_revision` | The **leaderboard baseline** `delta_vs_base` is measured against — display only. 🔴 Not where your training starts (that is `params.training`), and since 1.1.1 a change here does **not** block payment |
| `competition` | `params` | The season's spec verbatim: `fee`, `training` (`image` / `dataset` / `checkpoint`), `strategy_template`, `format`. Read, never rewritten by us — a new season adding a key needs no new CLI |
| `competition` | `params.training.dataset` | `{train, val}` — what this season trains on. `null` means the season has not published one, and `openroboto train` refuses rather than reaching for another season's data |
| `competition` | `params.training.checkpoint` | Where this season's training **starts**. `null` = the training image uses the base it was built around. 🔴 Not `base_repo`: that is the **baseline** the leaderboard measures you against, and for π0.5 the two were different addresses |
| `model` | `vla_checkpoint_path` | A base checkpoint you already have locally. Empty is normal; when the season names one, the season wins |
| `huggingface` | `token`, `username` | Local Hugging Face upload credentials |
| `huggingface` | `repo_id` | Optional, used verbatim when set. Empty = `<username>/<base_model_family>-<last 12 of hotkey>`, i.e. **one repository per season**. 🔴 Set it to keep a repository you already upload to — miners who started before 2026-09-02 have a `<username>/pi05-…`, and leaving this empty makes the next upload create a new repository and re-push several GB |
| `huggingface` | `merged_model_id` | Optional, informational; nothing in the submission path reads it |
| root | `custom_train_script` | Optional miner-owned training strategy path |
| `training` | `epochs`, `batch_size`, `learning_rate`, `lora_r`, `lora_alpha` | **Yours to tune** — that is the competition. They reach the container as `EPOCHS` / `BATCH_SIZE` / `LR` / `LORA_R` / `LORA_ALPHA`, which your strategy script reads out of `cfg`. Defaults are `3 / 4 / 1e-4 / 32 / 64`. ⚠️ Write the learning rate `1.0e-4`, not `1e-4` — YAML reads the second as text |
| root | `log_level`, `log_dir` | Local logging |

Nothing in this file is fetched. `openroboto train` opens no URL beyond the
dataset the season names: which competition, its status and its spec are all in
the `competition:` section, and the five hyperparameters above are yours.

> **The fee is `competition.params.fee`, and there is no other way to set one.**
> `openroboto submit` confirms it against the backend in the moment before paying
> — printing which season, how long it has left, how much and to whom, and asking.
> That check cannot be turned off, and `--force` does not skip it. A backend it
> cannot reach is a refusal, not a warning: without an answer there is no way to
> say who the money would be going to.
>
> An amount typed anywhere in this file is ignored. A number says how much, never
> *which competition*, and a fee paid with no season attached is filed under
> whichever season the backend defaults to — with the TAO already gone. A
> workspace with no `competition` section is refused rather than charged. There is
> deliberately no built-in default fee.

## Weight-setting validator

```bash
openroboto init --validator    # writes validator.yaml, no strategy script
```

| Section | Field | Purpose |
|---|---|---|
| root | `environment` | Same as for miners |
| `subnet` | `network`, `netuid` | Bittensor network and subnet |
| `subnet` | wallet fields | Local validator wallet selection |
| `urls` | `control_json` | Where the subnet publishes `public_key`. **Validators only** — see [VALIDATOR.md](./VALIDATOR.md#the-controljson-contract). No miner command opens it |
| `backend` | `url` | Read-only result service base URL |
| `backend` | `public_key` | Optional public read credential |
| root | `weight_interval_min` | Minutes between `set_weights` calls; default 60. Bounded on both sides by the chain and both bounds bite silently: below ~20 min (`weights_rate_limit`, 100 blocks) the extrinsic is rejected, and going quiet for more than ~16.7 h (`activity_cutoff`, 5000 blocks) makes the subnet treat your weights as absent |

The weight-setting validator does not accept a scoring or management credential.

## Secret handling

- Never commit `miner.yaml`, `validator.yaml`, `.env`, wallet files, tokens, or passwords.
- Keep example values empty or use explicit placeholders.
- Prefer an interactive wallet prompt when practical.
- Review `git status` and run the repository sensitive scan before publishing.
