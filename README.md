# openroboto

The command-line tool for mining on **OpenRoboto**, a Bittensor mainnet subnet
(netuid 80) that rewards improvements to vision-language-action models.

You fine-tune π₀.₅ on LIBERO, publish the checkpoint to Hugging Face, pay a small
on-chain evaluation fee, and announce it. The subnet evaluates every submission in
simulation with a seed nobody can predict, ranks the results, and pays emissions by
rank.

Everything you type is this one package. **There is nothing to clone.**

```bash
pip install openroboto
```

[Docs index](docs/README.md) · [How the subnet works](docs/SUBNET_OVERVIEW.md) ·
[Migrating from `rt.py`](docs/MIGRATION.md) ·
[Evaluation toolkit](https://github.com/openroboto-ai/openroboto-evaluation)

> **Upgrading from a clone of this repository?** `python miner.py` and
> `python rt.py submit` were removed on 2026-08-19. See
> [docs/MIGRATION.md](docs/MIGRATION.md) for the command map — your existing
> `miner.yaml` and `state/round_N.json` still work.

---

## Requirements

- Linux, an NVIDIA GPU (24 GB VRAM minimum) and a recent driver
- Python **3.11**
- Docker with the NVIDIA Container Toolkit — training runs in a container because
  openpi needs `numpy<2.0` and bittensor needs `numpy>=2.0`; one interpreter cannot
  hold both
- A registered Bittensor mainnet hotkey with enough TAO for the evaluation fee
- A Hugging Face account and a write token

## Your first submission

```bash
# 1. Install and scaffold
pip install openroboto
openroboto init my-miner          # a ready-to-use workspace: miner.yaml,
                                  # train_strategy.py, README.md, .gitignore
cd my-miner
$EDITOR miner.yaml                # hotkey_ss58, HF token + username, control.json URL

# 2. Check everything BEFORE anything costs money
openroboto doctor                 # GPU, Docker, HF permissions, balance, config

# 3. Build the training image, then train one round
openroboto build
openroboto train

# 4. Verify the checkpoint format — still free
openroboto check

# 5. Upload, pay the fee, announce on chain
openroboto submit

# 6. See what the subnet made of it
openroboto status
```

Steps 2 and 4 exist for one reason: **burns are not refundable.** The most expensive
mistake on this subnet is discovering after paying that the upload was not something
the evaluator can load — a bare LoRA adapter, or a checkpoint buried in a
subdirectory. `doctor` and `check` are free and catch both.

Full walkthrough: [docs/MINER.md](docs/MINER.md).
Real-machine setup, systemd, custom strategies: [docs/MINER_DEPLOY.md](docs/MINER_DEPLOY.md).

## Commands

| Command | What it does |
|---|---|
| `openroboto init [DIR] [-s simple\|example] [--validator]` | Create a working workspace: config, a training strategy to edit, a README with the exact next commands, and a `.gitignore` that keeps your wallet password out of git |
| `openroboto doctor` | Environment check: Python, config, `control.json`, Docker, GPU, image, HF token, wallet balance |
| `openroboto build` | Build the training image from the build context shipped inside the package (no clone, no network) |
| `openroboto train [-s script.py]` | Run one round; your strategy script is mounted into the container |
| `openroboto check [PATH]` | Verify checkpoint layout with the rules the evaluator uses — **no GPU, no network, no second repository** |
| `openroboto upload / burn / announce` | The three submission steps, individually — for recovery, not routine use |
| `openroboto submit [--force]` | All three, resumable from `state/round_N.json` |
| `openroboto status [--hotkey]` | Submission history and scanner rejection reasons (no API key needed) |
| `openroboto validator run` | External validator: read published weights, set them on chain |
| `openroboto --version` | CLI version and protocol package version |

⚠️ **There is no `openroboto merge`, and none is planned.** Exporting a full
checkpoint is part of **training** — it needs the model libraries, which cannot live
in the same interpreter as `bittensor` — so it belongs in your training script,
inside the training container. The bundled strategies leave that step blank on
purpose and say so; `openroboto train` tells you what the run actually produced, and
`openroboto check` gives the verdict **before you pay**.

## Things that will cost you TAO if you skip them

The fee is published live in `control.json`; on mainnet today it is 0.1 TAO. Never
hard-code it — the CLI reads it, and **refuses to burn if it cannot** rather than
guessing an amount the backend would reject.

**Do not run `burn` and `announce` as separate steps unless you are recovering.** The
backend only accepts a submission whose burn is within **50 blocks (~10 minutes)** of
the chain commitment; this stops a single fee being reused across submissions. Past
that window the submission is rejected and the fee is gone. `openroboto submit` runs
the three steps back-to-back so you stay inside it, and `announce` refuses to publish
once the window has closed instead of charging you a second fee for a doomed
submission.

Details: [docs/PAYMENT.md](docs/PAYMENT.md).

## Submission format

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

Your seed is derived from three public values that did not exist when you submitted —
the block hash that carried your commitment, the round number, and a drand beacon
value. Nobody, including the subnet operator, can pick a seed for a specific miner.
You can recompute it:

```bash
pip install openroboto-protocol
```

```python
from openroboto_protocol.seed import derive_seed

seed = derive_seed(block_hash, round_num, drand_randomness)
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
restarting a command that burns non-refundable TAO is not something to leave to a
restart policy.

⚠️ This compose file mounts the Docker socket so the containerised CLI can start
the training container. That grants host root to the container; run it only on
your own machine. Installing with `pip` avoids that entirely, which is why it is
the documented path.

## Public trust boundary

Public: miner participation, local training, Hugging Face upload, burn and chain
announcement; chain commitment formats and weight-setting logic; evaluation rules,
baseline methodology, LIBERO tooling and seed derivation; the miner-visible
`control.json` schema and the read-only API contract.

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
which is how the pin once read `==1.0.0` while every local and CI run was actually
using `0.2.0`.

`--locked` is deliberate: it fails when `uv.lock` no longer matches `pyproject.toml`
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

Both pre-releases and stable releases go to PyPI, and the version number is what
separates them:

```bash
git tag v0.1.0a2 && git push origin v0.1.0a2    # internal testing build
git tag v0.1.0   && git push origin v0.1.0      # stable
```

The tag triggers the same gates every pull request runs, and the `pypi`
environment only accepts `v*` tags, so a stray branch build cannot reach the
index.

It then waits for a reviewer to approve the `pypi` environment. Uploads cannot be
undone and a version number can never be reused, so that approval is the last
thing standing between a typo and every miner.

Testers install a pre-release by pinning it exactly — no extra index flags:

```bash
pip install openroboto==0.1.0a2
```

pip does not pick pre-releases when a stable release exists, so a miner running
`pip install openroboto` will never land on one.

> ⚠️ **One exception, while it lasts.** `openroboto` has no stable release yet,
> and pip *does* select a pre-release when nothing stable is available. Until
> `0.1.0` ships, an alpha on PyPI is what a bare `pip install openroboto` gets.
> Either publish `0.1.0` early, or yank the alphas once testing is done — a
> yanked version is skipped by the resolver unless pinned exactly, and anyone who
> already installed it keeps working.

`workflow_dispatch` publishes to TestPyPI instead. That path exists to rehearse
the pipeline itself — `uv publish`, the OIDC exchange, the approval gate — not to
distribute test builds. It can never reach real PyPI.

## Repository map

| Path | Purpose |
|---|---|
| `src/openroboto/` | The package: `commands/`, `chain/`, `huggingface/`, `payment/`, `config/`, `training/`, `templates/` |
| `src/openroboto/runner/` | Training-image build context (Dockerfile + the in-container entry script), **shipped in the wheel** so `openroboto build` works offline |
| `docs/` | Miner, validator and reproducibility documentation — index at [docs/README.md](docs/README.md) |
| `tests/` | Mirrors `src/`; needs no GPU, chain or network |
| `Dockerfile`, `docker-compose.yml` | Optional containerised way to run the CLI |

The old flat layout (`rt.py`, `miner.py`, `payment.py`, `validator.py`, `miner/`,
`utils/`, `protocol/`) was removed on 2026-08-19; the package replaces all of it. It
remains in git history, and [docs/MIGRATION.md](docs/MIGRATION.md) maps every old
command to its replacement.

Local configuration, runtime state, logs, caches and model weights are excluded by
`.gitignore`.

## License

See [LICENSE](LICENSE).
