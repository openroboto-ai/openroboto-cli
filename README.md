# openroboto

The command-line tool for mining on **OpenRoboto**, a Bittensor mainnet subnet
(netuid 80) that rewards improvements to vision-language-action models.

You fine-tune the current season's base model — LingBot-VLA 2.0 today — publish the
checkpoint to Hugging Face, pay a small on-chain evaluation fee, and announce it. The subnet evaluates every submission in
simulation with a seed nobody can predict, ranks the results, and pays emissions by
rank.

Task-authoring seasons accept task packages through the same CLI. See
[Task authoring](#task-authoring) for their workspace and submission workflow.

Everything you type is this one package. **There is nothing to clone.**

```bash
pip install openroboto
```

[Docs index](docs/README.md) · [How the subnet works](docs/SUBNET_OVERVIEW.md) ·
[This season's base model](docs/MIGRATION.md) ·
[Evaluation toolkit](https://github.com/openroboto-ai/openroboto-evaluation)

Real-robot documentation: [parallel tracks](docs/REAL_TRACKS.md) ·
[miner preparation](docs/MINER_REAL.md) · [workstation and reference data](docs/WORKSTATION_DATA.md).
These pages describe π0.5 and LingBot as parallel physical tracks and publish the
confirmed joint-space interface. Publication does not announce a new competition
as open.

---

## Task authoring

This section describes the 1.9.0 workflow on `main`. Version 1.9.0 is not yet
published to PyPI; see the [changelog](CHANGELOG.md) for release status.

Task and model seasons use the same top-level workflow. Choose a task season:

```bash
openroboto init my-task-workspace
cd my-task-workspace
# Edit task.json using catalog.json and add solve.py.
openroboto check                 # v1 demo: check --example
openroboto submit
```

The CLI writes a workspace for the season's `competition.track` and execution
profile. Controlled Panda v1/v2 tasks and schema-3 imported scenes use these same
commands. `check` automatically prepares the official bundled CPU Docker image,
builds the package, records missing reference actions and runs worker
structural/physical admission. MuJoCo and the worker stay inside the container.
No validator checkout, registry login or Python runtime path is needed.
`check --static-only` runs without Docker; it does not produce a physical passing
verdict.

A successful full check records a digest-bound receipt. Before payment, `submit`
checks that receipt against the live season's policy and verifies the uploaded
`repo@commit`. Scene submissions also require the exact active execution contract
published by the backend. Local success and server admission are separate results.

Task seasons can publish a fixed Gamma fee in `params.fee.amount_gamma`. Submit
spends the miner-owned deposit locally after all free gates and confirmation,
then verifies its exact on-chain payment event. `doctor` checks the Gamma balance
and authorization. An interrupted submission reconciles the original transaction
without charging again. See [Gamma preparation](docs/TASK_PACKAGES.md#paying-a-gamma-submission-fee).

`catalog`, `create`, `record-reference` and `self-test` are top-level authoring
verbs. `build`, `check`, `doctor` and `status` route by `competition.track`.
The task command group has been removed. Read the
[task package guide](docs/TASK_PACKAGES.md) for objects, ranges, reference scripts,
image identities, JSON verdicts and the pre-payment gate.

For schema-3 scenes, `init` writes the scene, declared assets and catalog. `check`
prepares execution root E and records missing reference actions for every initial
state into a matching private root P. Any change to E requires fresh reference
actions. Ordinary `submit` recognizes both roots automatically and registers their
identity with the backend. If chain ingestion is pending,
rerun `submit` to finish registration without paying again. The explicit `scene`
commands remain available for advanced inspection and independently installed
runtimes; see the [scene guide](docs/TASK_PACKAGES.md#9-author-owned-scenes-schema-3).

## Model training requirements

- Linux, an NVIDIA GPU (24 GB VRAM minimum) and a recent driver
- Python **3.11**
- Docker with the NVIDIA Container Toolkit — training runs in a container because
  openpi needs `numpy<2.0` and bittensor needs `numpy>=2.0`; one interpreter cannot
  hold both
- A registered Bittensor mainnet hotkey with enough TAO for the evaluation fee
- A Hugging Face account and a write token

## Your first model submission

```bash
# 1. Install and scaffold
pip install openroboto
openroboto init my-miner          # a ready-to-use workspace: miner.yaml,
                                  # train_strategy.py, README.md, .gitignore
cd my-miner
$EDITOR miner.yaml                # hotkey_ss58, HF token + username

# 2. Check everything BEFORE anything costs money
openroboto doctor                 # config, season + fee, HF permissions, balance, Docker, GPU

# 3. Build the training image, then train
openroboto build
openroboto train

# 4. Verify the checkpoint format — still free
openroboto check

# 5. Upload, pay the fee, announce on chain
openroboto submit

# 6. See what the subnet made of it
openroboto status
```

Steps 2 and 4 exist for one reason: **entry fees are not refundable.** The most expensive
mistake on this subnet is discovering after paying that the upload was not something
the evaluator can load — a bare LoRA adapter, or a checkpoint buried in a
subdirectory. `doctor` and `check` are free and catch both.

Full walkthrough: [docs/MINER.md](docs/MINER.md).
Real-machine setup, systemd, custom strategies: [docs/MINER_DEPLOY.md](docs/MINER_DEPLOY.md).

## Commands

| Command | What it does |
|---|---|
| `openroboto init [DIR] [-s simple\|example] [--validator] [--refresh] [--backend-url URL] [--force]` | Create a track-specific workspace: config, a training strategy or task definition and catalog, a README with the next commands, and a `.gitignore`. Task seasons are listed by default. ⚠️ **This command queries the backend**: the open competitions come from `GET /api/v1/competitions`, and if that cannot be reached it writes **no file at all** rather than guessing a season. `--refresh` rewrites only the `competition:` section of an existing `miner.yaml`; `--backend-url` says which backend to ask (and the whole workspace is then written to match it); `--force` overwrites existing files |
| `openroboto doctor` | Check config, the season's spec and fee, HF permissions, wallet balance and the selected track's environment: model GPU/training image, or task package/profile, scene execution-contract snapshot and official self-test image |
| `openroboto build` | Build the training image, or the task package and official self-test image, from the bundled context (no clone). Refuses for a competition this client has no image for, rather than filling that competition's image name with the π0.5 one |
| `openroboto train [-s script.py]` | Train once; your strategy script is mounted into the container |
| `openroboto check [PATH] [--static-only] [--json]` | Route by `competition.track`: validate a model checkpoint's layout, or run task structural/reference/physical admission in the official container. Task `--static-only` checks structure without Docker and does not produce a physical passing receipt |
| `openroboto catalog` | Show the configured task profile's robots, objects and complete parameter ranges; `init` also writes them to `catalog.json` |
| `openroboto create PATH` | Create a task definition for the workspace profile; `--from-execution E` imports an existing scene into new inputs while retaining the original E |
| `openroboto record-reference [--script solve.py]` | Record reference actions for every instance in the official container; the new reference changes the package identity |
| `openroboto self-test` | Run the task package's physical admission in the official container |
| `openroboto identity keygen \| show \| register \| bind \| unbind \| migrate` | The persistent contributor identity an authored task is paid to. `keygen` writes an Ed25519 key to `~/.openroboto/contributor.key` (mode 0600); `register` signs a `register` message; `bind` binds the hotkey in `miner.yaml` with **two** signatures (contributor key + hotkey sr25519). `submit` refuses to pay for a task-track entry from an unbound hotkey. See [CONTRIBUTOR.md](docs/CONTRIBUTOR.md) |
| `openroboto submit [--config] [--output-dir] [--force]` | Upload → re-check the live season and fixed repository content → pay → announce. Task submissions also require a matching check receipt; scene submissions verify the live execution contract and register both roots automatically. Resumable from `state/competition_<id>.json`: an upload already done is not repeated and a fee already paid is not paid again |
| `openroboto status [--hotkey] [--competition]` | Route by the configured track: model submission history/scanner rejections, or signed task submission and backend admission status |
| `openroboto validator run` | External validator: read published weights, set them on chain |
| `openroboto --version` | CLI version and protocol package version |

⚠️ **There is no `openroboto merge`, and none is planned.** Exporting a full
checkpoint is part of **training** — it needs the model libraries, which cannot live
in the same interpreter as `bittensor` — so it belongs in your training script,
inside the training container. The bundled strategies leave that step blank on
purpose and say so; `openroboto train` tells you what the run actually produced, and
`openroboto check` gives the verdict **before you pay**.

⚠️ **`openroboto submit` repeats the model layout check before payment.** Between
the upload and the payment it reads the file listing of your HuggingFace
repository — the same listing the subnet reads *after* the fee — and stops
without paying anything if that listing would not earn a score. There is no flag
to skip it, because past that point a rejection is final and the TAO is not
refunded. If the listing cannot be fetched at all, it also stops: an answer we
could not get is not one this will spend your money on. Run `openroboto check`
first anyway — it is free, it runs before the multi-gigabyte upload, and it
checks two more rules that need the weight index on your disk.

## Things that will cost you TAO if you skip them

The fee is published on the competition you are entering (`params.fee`), and the
simulation and real-hardware seasons do not charge the same thing, which is why no
figure belongs in your config — or in this file. Never hard-code it:
`openroboto submit` confirms it against the backend in the moment before paying,
and **refuses to pay at all** if it cannot, rather than reaching for an amount
that says how much but not which competition.

**`submit` is the only command that pays, and that is on purpose.** The backend only
accepts a submission whose payment is within **50 blocks (~10 minutes)** of the chain
commitment; this stops a single fee being reused across submissions. Past that window
the submission is rejected and the fee is gone. `openroboto submit` runs upload,
payment and announcement back-to-back so you stay inside it, and it refuses to
publish once the window has closed instead of charging you a second fee for a doomed
submission. `upload` / `burn` / `announce` were removed as separate commands in 1.0
for exactly this reason: running one alone is how a fee gets paid for a submission
that is never announced.

Details: [docs/PAYMENT.md](docs/PAYMENT.md).

## Model submission format

The evaluator accepts **complete model checkpoints** — an openpi JAX `params/`
directory or a PyTorch `model.safetensors`, plus
`assets/physical-intelligence/libero/norm_stats.json`. A bare LoRA adapter is rejected
by a CPU pre-check before any GPU time is spent.

Put them at the **top** of the directory you upload. The evaluator descends two
levels looking for the weights and stops; the LingBot exporter's own layout,
`checkpoints/global_step_N/hf_ckpt/`, is one level past that, so an unedited upload
of a training output is admitted and then never loaded.

Exact requirements: [docs/SUBNET_OVERVIEW.md](docs/SUBNET_OVERVIEW.md).

## Verify your evaluation seed

Your seed is derived from three public values, two of which did not exist when you
submitted — the block hash that carried your commitment, the **competition id** your
submission was admitted to, and a drand beacon value. Nobody, including the subnet
operator, can pick a seed for a specific miner. You can recompute it:

```bash
pip install openroboto-protocol
```

```python
from openroboto_protocol.seed import derive_seed

# The middle argument is the competition id, not the payload's `r` and not the
# season ordinal the API displays. It is passed by position.
seed = derive_seed(block_hash, competition_id, drand_randomness)
```

The backend and this CLI import that exact function — not a copy of it.
Formula, drand chain identifier and security assumptions:
[docs/SEED_GENERATION.md](docs/SEED_GENERATION.md).

## Docker: what runs where

Training **always** runs in a container — openpi needs `numpy<2.0` and bittensor
needs `numpy>=2.0`, so one interpreter cannot hold both. You do not have to
arrange that:

```bash
openroboto build     # builds the training image, once
openroboto train     # starts it for you, data and strategy mounted in
```

The training image definition ships **inside the package**. There is nothing to
clone, nothing to keep in sync, and no network needed to build it. You do need
Docker on the host.

The package ships **two** image definitions, one per base model: `runner/`
(openpi, π0.5) and `runner/lingbot/` (LingBot-VLA 2.0). Which one is used follows
`competition.base_model_family`, never the adapter name. A competition whose base
model has no image here is refused by `build` and `train` — they stop rather than
build another base model's image under that competition's name. Train such a season
your own way, then come back for `openroboto check` and `openroboto submit`, which
both work on a checkpoint this CLI did not produce.

### Running the CLI in a container too (optional, needs a clone)

Separately, the `Dockerfile` and `docker-compose.yml` **in this repository** put
the CLI itself in a container, for people who would rather not install it into
their host Python. That is a repository-level convenience: `openroboto init` does
**not** write these files into your workspace, because a compose file that builds
the CLI image would need this repository's build context anyway.

```bash
git clone https://github.com/openroboto-ai/openroboto-cli && cd openroboto-cli
docker compose up train
docker compose run --rm train submit --config miner.yaml
```

There is deliberately no `submit` service — a compose service can be restarted, and
restarting a command that spends a non-refundable entry fee is not something to leave
to a restart policy.

⚠️ This compose file mounts the Docker socket so the containerised CLI can start
the training container. That grants host root to the container; run it only on
your own machine. Installing with `pip` avoids that entirely, which is why it is
the documented path.

## Public trust boundary

Public: miner participation, local training, Hugging Face upload, fee payment and
chain announcement; chain commitment formats and weight-setting logic; evaluation rules,
baseline methodology, LIBERO tooling and seed derivation; and the read-only API
contract. (`control.json` is documented for external validators, who fetch
`public_key` from it — no miner command reads it.)

Not here: held-out task data, the scoring-service deployment, and subnet-owner
operational tooling.

Seed derivation is public precisely because publishing it gives nothing away — the
future block hash and drand value do not exist at submission time.

## Development

For contributors. Skip this if you installed from PyPI.

```bash
git clone https://github.com/openroboto-ai/openroboto-cli
cd openroboto-cli
uv sync --locked
```

One repository is enough: `openroboto-protocol` is installed from PyPI at the exact
version pinned in `pyproject.toml`. To work against unreleased protocol changes,
override it in your environment only —
`uv pip install -e ../openroboto-protocol` — and do not commit a
`[tool.uv.sources]` path entry. A path source **bypasses the version constraint**,
A path source that disagrees with the pin leaves every local and CI run green while
resolving a different version than the one published.

`--locked` is deliberate: it fails when `uv.lock` does not match `pyproject.toml`
instead of silently resolving a different dependency tree. The interpreter is pinned to
Python 3.11 by `.python-version` — the version miners run.

```bash
bash scripts/lint.sh                             # mypy + ruff check + ruff format
uv run pytest -q                                 # no GPU, no chain, no network
uv run coverage run --source=src -m pytest -q
uv run coverage report                           # fails below the threshold in pyproject.toml
uvx pre-commit install                           # optional: the same lint on every commit
```

`.github/workflows/ci.yml` runs these same commands — `scripts/lint.sh` is the single
definition of "lint", so local and CI cannot drift. CI also fails on any skipped test:
nothing here needs hardware or credentials, so a skip means a test was switched off.

Protocol constants (commitment encoding, seed derivation, shared vocabularies) come
from `openroboto-protocol` and are never copied in here.
`.github/workflows/protocol-guards.yml` enforces that and that the dependency is pinned
to an exact version — a floating range would let the miner side and the backend side
resolve different code, which is the one thing that package exists to prevent.

## Releasing

```bash
git tag v1.0.1   && git push origin v1.0.1      # stable
git tag v1.1.0a1 && git push origin v1.1.0a1    # pre-release, for testing
```

The tag triggers the same gates every pull request runs, and the `pypi`
environment only accepts `v*` tags, so a stray branch build cannot reach the
index. It then waits for a reviewer to approve that environment: uploads cannot
be undone and a version number can never be reused, so that approval is the last
thing standing between a typo and every miner.

`pip install openroboto` resolves to the newest **stable** release. A tester
takes a pre-release by pinning it exactly (`pip install openroboto==1.1.0a1`);
no miner lands on one by accident.

## Repository map

| Path | Purpose |
|---|---|
| `src/openroboto/` | The package: `commands/`, `chain/`, `huggingface/`, `payment/`, `config/`, `training/`, `templates/` |
| `src/openroboto/runner/`, `src/openroboto/runner/lingbot/` | One training-image build context per base model (Dockerfile + the in-container entry script), **both shipped in the wheel** so `openroboto build` works offline |
| `docs/` | Miner, validator and reproducibility documentation — index at [docs/README.md](docs/README.md) |
| `tests/` | Mirrors `src/`; needs no GPU, chain or network |
| `Dockerfile`, `docker-compose.yml` | Optional containerised way to run the CLI |

Local configuration, runtime state, logs, caches and model weights are excluded by
`.gitignore`.

## License

See [LICENSE](LICENSE).
