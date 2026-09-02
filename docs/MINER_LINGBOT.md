# Miner Guide — LingBot-VLA 2.0 (simulation competition)

> **Status**: ✅ **live — the competition opened 2026-09-01 and takes submissions.**
> `build` and `train` work for it; what this repository still does not ship is a
> LingBot *training configuration* template, so the export step is described rather
> than handed to you (§5). ·
> **Updated**: 2026-09-02 · **Audience**: miners moving from the π0.5 competition
> **Scope**: install → base model → check → upload → fee → chain announcement.
> **Note**: the π0.5 simulation season is archived — its final board stays on the
> site (season switcher on the leaderboard). [MINER.md](./MINER.md) remains for
> reference; machine setup is [MINER_DEPLOY.md](./MINER_DEPLOY.md); every
> `miner.yaml` field is [CONFIG.md](./CONFIG.md).

Every command below was run before it was written down. The ones that do not work
today say so, in place, with what they are waiting on — so that when one stops you,
you know it is not your machine.

## 0. What works today, and what does not

Measured 2026-09-02 against `https://api.openroboto.ai` and against
`openroboto 1.2.0 (openroboto-protocol 0.9.0)`. **Upgrade first**: this
competition needs `openroboto >= 1.2.0`. 1.0.x speaks the retired round vocabulary
and cannot resolve this season at all; 1.1.x resolves it but still names your
HuggingFace repository `pi05-…` whatever season you are on, which is what 1.2.0
fixed.

| Step | Today | Blocked on |
|---|---|---|
| `pip install -U openroboto` | ✅ works (Python ≥ 3.11) | — |
| `openroboto --version` | ✅ works | — |
| `openroboto init` | ✅ **works** — lists both open competitions, writes a LingBot workspace (`cid=2`) | — |
| Downloading the base model | ✅ works — `openroboto-ai/lingbot-vla-v2-6b-libero`, 75 files, 25.5 GB (§4) | — |
| **Training** | ✅ the container builds and runs (verified on an A100-SXM4-80GB, 2026-08-26). What is still missing is a **training configuration template** in this repository, so §5 tells you what to get right rather than handing you a config | a LingBot training config template |
| `openroboto build` / `openroboto train` | ✅ work — `runner/lingbot/` ships in the wheel and is selected by `competition.base_model_family` | — |
| `openroboto check` | ✅ works | — |
| `openroboto doctor` | ✅ works | — |
| `openroboto submit` | ✅ unblocked — resolves the season, confirms the 0.1 TAO fee against the backend before paying | — |
| `openroboto status` | ✅ works | — |

The catalogue blocker from the 2026-08-26 revision of this page is gone —
production serves the competitions list and `init` was re-run against it for
this revision:

```bash
curl -s https://api.openroboto.ai/api/v1/competitions
# … "id":2,"track":"sim","seq":2,"label":"LingBot-VLA 2.0","status":"active" …

openroboto init my-miner
# Competitions taking submissions:
#   1. xArm 6 · π0.5 (real/1 · cid=3)
#   2. LingBot-VLA 2.0 (sim/2 · cid=2)
# Which one? [1-2]
```

---

## 1. Compared with π0.5

### What does **not** change — most of it

If you have mined the π0.5 competition, this is the part to read first.

| | Still true | Where it is decided |
|---|---|---|
| The exam | **LIBERO**, the same task suites in simulation. Only the textbook changed | [SUBNET_OVERVIEW.md](./SUBNET_OVERVIEW.md) §6 |
| What you deliver | Upload to HuggingFace, then announce on chain. Two artefacts, same order | `commands/submit.py::run` |
| Your repository name | `{hf-username}/lingbot-vla-2.0-{last 12 chars of your hotkey}` — one repository per season. Already have a `pi05-…` one? Put it in `huggingface.repo_id` and keep using it. | `huggingface/repository.py::build_repo_id` |
| One repository per **season** | Within this season, round N is uploaded on top of rounds 1..N-1 — `upload_folder` never deletes. It is no longer one repository for your whole career | `huggingface/upload.py::push_model`, `huggingface/repository.py` |
| Entry fee | **0.1 TAO**, and this season's `params.fee.kind` is `burn` — destroyed, not transferred. (The real-hardware track transfers instead; see [PAYMENT.md](./PAYMENT.md)) | competition row `sim/2` in `0003_competitions.sql` |
| Chain announcement | Same encoder, **≤512 bytes**, burn→announce within **50 blocks** | `preflight.py::check_burn_window`, `openroboto_protocol.commitment.MAX_COMMITMENT_BYTES` = 512, `constants.BURN_BLOCK_WINDOW` = 50 |
| Command sequence | `init → build → train → check → submit` | unchanged |
| 10 MB floor · ≤2 levels of nesting · the leftover-file rules | Identical, rule for rule, to the π0.5 checker | `openroboto_protocol.model_format`: `MIN_TOTAL_SIZE_BYTES`, `MAX_CHECKPOINT_NESTING_DEPTH`, `_scan_files` |

Your wallet, your hotkey, your HuggingFace account, your `miner.yaml` field names,
your `state/round_N.json` — none of it is touched.

### What does change

| | π0.5 | LingBot-VLA 2.0 |
|---|---|---|
| Files you submit | weights + `assets/physical-intelligence/libero/norm_stats.json` | weights (sharded) + `config.json` + `model.safetensors.index.json`. **No `norm_stats`** — nothing looks for it |
| Who downloads the base model | the CLI's training container does it for you | **you do**, by hand — see §4 |
| Gate before payment | none; `openroboto check` was voluntary | **mandatory and unskippable.** A warning also refuses |

The last one is worth a paragraph, because it changes what a mistake costs.
`submit` judges your HuggingFace file listing *before* it pays, and there is
no `--skip-check`; `--force` does not skip it either. And it confirms with the
backend which competition the fee is for, what the fee is, and who receives it,
before it asks you to confirm. If the backend cannot be reached, it **refuses to
pay** rather than paying on an assumption.

> 🔴 **`submit` does not compare `(base_repo, base_revision)`, and four places on
> this page used to say it did.** That gate was removed on 2026-09-01 (1.1.1) because
> it watched the wrong field: `base_repo` is the **leaderboard baseline** that
> `delta_vs_base` is measured against, not where your training starts (that is
> `params.training`). When operations repointed the baseline at the repository
> carrying the evaluation results, every already-initialised miner was blocked from
> paying — and told to retrain, which was false. What genuinely does invalidate a
> training run is a changed *starting point*, and no gate watches that yet.

---

## 2. Install

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install openroboto
openroboto --version
```

Expect the client and the protocol package on one line:

```
openroboto 1.2.0 (openroboto-protocol 0.9.0)
```

**1.2.0 is the minimum for this competition.** 1.1.0 was the release that resolves
seasons by competition id and model name — a 1.0.x client speaks the retired round
vocabulary and stops at the season lookup — but 1.1.x still derives the HuggingFace
repository name as `{username}/pi05-{hotkey suffix}` no matter which season it is
on. That name outlived the base model it was named after: on 2026-09-02 eight queued
submissions all read `<user>/pi05-…` while every one of them held a LingBot model,
and three people in a row concluded miners had submitted the wrong base. 1.2.0 names
the repository after the season's base model. Already installed? `pip install -U
openroboto`.

Machine preparation (NVIDIA driver, Docker, the container toolkit, systemd) has
not changed: [MINER_DEPLOY.md](./MINER_DEPLOY.md) §1 and §8.

---

## 3. Create the workspace

```bash
openroboto init my-miner && cd my-miner
# Which one? [1-2]  → pick "LingBot-VLA 2.0 (sim/2 · cid=2)"
```

`init` asks the backend which competitions are open, you pick one, and the whole
spec of that season is written into `miner.yaml`: base model and revision,
training image, checkpoint layout rules, entry fee, deadlines. Every later command
reads that snapshot off disk, which is why `build` / `train` / `check` never need
the network.

There is no `--track` flag and no new subcommand — which competition you mine is
a value in your config, not something you type each time.

**One thing to watch**: the official base model finished uploading on 2026-09-01
and the season record was repinned to it (§4). A workspace that predates the repin
holds a stale snapshot — refresh it with `openroboto init --refresh`. ⚠️ Nothing
refuses payment over it: the `(base_repo, base_revision)` comparison was removed in
1.1.1 (see §1), and what those two columns hold is the leaderboard baseline, not
your training starting point.

> 🔴 **An older `miner.yaml` with no `competition:` section does not work any
> more.** `openroboto submit` refuses it before uploading anything, because a fee
> paid with no season attached is filed under whichever season the backend defaults
> to and is not refunded. `openroboto init --refresh` writes that section and leaves
> every other line of the file byte for byte as it is.

---

## 4. Get the base model — you download this one yourself

π0.5's checkpoint was fetched by the training container. LingBot's is not: you
fetch it, once, and point your training at it.

**The official base model is `openroboto-ai/lingbot-vla-v2-6b-libero`** —
LingBot-VLA 2.0 already fine-tuned on LIBERO. Upload completed 2026-09-01:
75 files carrying the sharded weights, tokenizer and processor configs, the
exact training configs and `norm_stats.json` under `training/`, per-suite
evaluation results under `evaluation/`, and `SHA256SUMS` with provenance
records. The pinned revision is
`ce6a322157acc7a03d0ca71bb84423c7f2e124d7` — confirm it against what your
season pins before you burn GPU time:

```bash
curl -s https://api.openroboto.ai/api/v1/competitions | python3 -m json.tool
# … "base_repo": …, "base_revision": … for sim/2
```

```bash
hf download openroboto-ai/lingbot-vla-v2-6b-libero \
  --revision ce6a322157acc7a03d0ca71bb84423c7f2e124d7
```

Its upstream parent, `robbyant/lingbot-vla-v2-6b`, is the raw LingBot-VLA 2.0
release, untrained on LIBERO — on this benchmark it starts from close to zero,
so starting there means rebuilding skills the official base already has.

Both repositories are **public and not gated** — no token, no access request.

`hf` is the HuggingFace CLI, installed with `openroboto` as part of
`huggingface_hub`. On `huggingface_hub` 1.x the older `huggingface-cli` name is
gone; if you have it in your notes, it now answers
`` `huggingface-cli` is deprecated and no longer works. Use `hf` instead. ``

**Pin the revision.** The backend records which base model and which commit this
competition is measured against, and a mismatch is a scoring problem, not a payment
one: ⚠️ **nothing refuses payment over it** — the gate that used to is gone (1.1.1,
see §1). Getting the revision right is on you, and it is worth the two seconds
before a week of GPU time.

### How much disk

Check before you start, not at 90%:

```bash
hf download openroboto-ai/lingbot-vla-v2-6b-libero \
  --revision ce6a322157acc7a03d0ca71bb84423c7f2e124d7 --dry-run
# [dry-run] Will download 75 files (out of 75) totalling 25.5G.
```

That 25.5 GB (23.8 GiB) is the six fp32 weight shards — 6.38 B parameters —
plus kilobyte-scale configs. Nothing to trim: the official repo does not carry
the upstream's distillation teachers.

Fetching the **upstream parent** too? Its numbers, measured the same way:

```bash
hf download robbyant/lingbot-vla-v2-6b \
  --revision 11c703bf6a5c1f45b3b69168482da11fdbba53d7 --dry-run
# [dry-run] Will download 23 files (out of 23) totalling 28.2G.
```

| What | Size |
|---|---|
| The six weight shards | **25.5 GB** (23.8 GiB) — 6.38 B parameters, all fp32 |
| Everything else in the repo | 2.7 GB — `depth/model.pt` (1.3 GB), `dino_video/teacher_step_10000.pth` (1.4 GB), tokenizer, three README images |
| **What the command above actually downloads** | **28.2 GB** (26.3 GiB) |

Files land in `~/.cache/huggingface/hub` unless you pass `--local-dir`. Budget for
the base model **plus** whatever your training run writes — a full-precision
checkpoint of this model is another 25 GB every time you save one, and the export
in §5 writes a second copy in HuggingFace format next to it.

If you know you do not need the distillation teachers, dropping them saves 2.7 GB
and lands you on exactly the weights:

```bash
hf download robbyant/lingbot-vla-v2-6b \
  --revision 11c703bf6a5c1f45b3b69168482da11fdbba53d7 \
  --exclude "depth/*" --exclude "dino_video/*" --exclude "assets/*"
# [dry-run] Will download 17 files (out of 17) totalling 25.5G.
```

Check against your own training config before trimming — `depth/` and
`dino_video/` are used by the vendor's own depth and video distillation settings.

> **Repeat the flag; do not list patterns after it.** `--exclude` and `--include`
> take **one** pattern each. `--include "*.safetensors" "*.json"` does not mean
> both — the second pattern is parsed as a positional filename, and the command
> then quietly downloads 9 files totalling 14.4 MB with **not one weight shard
> among them**. It exits 0. Add `--dry-run` and read the total before you commit
> to a download this size.

---

## 5. Train

`openroboto build` and `openroboto train` work for this competition:

```bash
openroboto build
openroboto train
```

The package ships two build contexts — `runner/` (π0.5) and `runner/lingbot/` — and
which one is used follows `competition.base_model_family`, never the adapter name.
The LingBot one was verified by a real run on an A100-SXM4-80GB (2026-08-26): the
container builds, the model loads with every parameter filled from the released
checkpoint, LoRA attaches to 396 real modules, and merge-and-export writes a flat
checkpoint root. Seven of the vendor's own defaults had to be overridden to get
there; each is commented at its call site in `runner/lingbot/train_runner.py`.

⚠️ **Measured peak was 12.4 GiB — weights only, before any batch.** The 14–18 GiB
figure is weights plus activations and remains arithmetic: the verification builds
and exports, it does not run a training step. A 24 GB card has 11.6 GiB of headroom
for activations, and whether that is enough is a number miners report back on.

**What this repository still does not ship is a LingBot training *configuration*
template**, so the two subsections below are the parts to get right yourself rather
than a config to copy. An invented one here would cost a week of GPU time on the
wrong shape.

- Whatever you train with, `openroboto check` and `openroboto submit` work on a
  checkpoint this CLI did not produce. Train it your own way and come back at §6.
- **There is no `openroboto merge`, and there will not be one.** Exporting a full
  checkpoint is part of training. A bare LoRA adapter is rejected.

### Two things about the vendor's training scripts, before you configure a run

Both are cheap to get right up front and expensive to discover afterwards.

**The HuggingFace export is on by default — but it lands three levels deep.**
`save_hf_weights` defaults to `True`
(`lingbotvla/utils/arguments.py`, `TrainingArguments.save_hf_weights`), so a run
started from either official template does write HuggingFace-format weights even
though neither template mentions the flag. The problem is *where*: the exporter
writes to `os.path.join(checkpoint_path, "hf_ckpt")`
(`lingbotvla/utils/async_hf_checkpoint.py`, `_run_hf_checkpoint`), and
`checkpoint_path` is `{output_dir}/checkpoints/global_step_N`. So you get

```
checkpoints/global_step_50000/hf_ckpt/
    model-00001-of-00006.safetensors … model-00006-of-00006.safetensors
    model.safetensors.index.json
    config.json  tokenizer.json  vocab.json  …
```

— **three levels down, and the evaluator searches two.** The vendor's own
post-trained artifact `robbyant/lingbot-vla-v2-6b-robotwin` is laid out this way
too, so uploading the training output unchanged is the normal way to end up here.
§6 catches it and prints the fix.

**An export failure stops the run; it does not go into a log file.**
`async_save_hf_weights` defaults to `False`
(same file, `TrainingArguments.async_save_hf_weights`), which takes the
synchronous branch of `AsyncHFCheckpointSaver.submit()` — called with
`best_effort=False`, so a failure is re-raised and the training command fails.
`async_hf_failures.jsonl` is written on the *asynchronous* path; on the default
configuration, a run that exits cleanly has exported. Verify the shards are
really there anyway, with §6 — it is free.

---

## 6. Check the checkpoint — free, local, no GPU, no network

```bash
openroboto check                     # this round's output directory
openroboto check path/to/checkpoint  # or point it somewhere
```

It applies **the same rules the subnet applies after you pay**, from
`openroboto_protocol.model_format` — not a second copy of them. Which rule book it
uses comes from the competition in your `miner.yaml`, never from sniffing the
directory, and it prints which one it used.

What a LingBot checkpoint needs at the **top** of the directory:

| Required | |
|---|---|
| `*.safetensors` shards | the weights. Not `adapter_model.safetensors` — a bare adapter is rejected, nothing merges it |
| `model.safetensors.index.json` | the `{tensor: shard}` map. Without it, shards alone are not recognised as a LingBot checkpoint |
| `config.json` | upload the whole checkpoint directory, not only the weight files |
| ≥ 10 MB total | below that, your weights are git-lfs pointers rather than the real files |
| ≤ 2 levels deep | see below |
| No `.git/`, no `.cache/`, no `*.tmp` / partial uploads | leftover upload state is a rejection |

**`norm_stats.json` is not on this list.** π0.5 required it; the LingBot rules
never look for it. Leaving it in does no harm, and leaving it out is correct.

A passing run:

```
checkpoint: ckpt
rules: LingBot-VLA 2.0
weights: pytorch
counted size: 12.0 MB
✅ Format check passed, you can run `openroboto submit`
```

And the failure you are most likely to hit — the vendor's own layout, uploaded
unchanged:

```
⚠️  [nested_too_deep] the checkpoint is nested 3 levels deep; the evaluator only searches 2 levels below the repo root
   → Your weights are in: checkpoints/global_step_50000/hf_ckpt/
     …
     Upload that directory as the repository root instead:
       openroboto check nested/checkpoints/global_step_50000/hf_ckpt
       openroboto submit --output-dir nested/checkpoints/global_step_50000/hf_ckpt

The subnet would accept this upload -- it is the evaluator that cannot load it.
That is worse than being rejected: by the time it fails, the TAO is burned and the queue slot is used.
```

Read that last pair of lines carefully. `nested_too_deep` is a **warning** to the
subnet's admission and a **stop** here, and the difference is the point:
admission answers "does this submission count", `check` answers "will the money
you are about to spend buy you a score". `check` exits non-zero on warnings too.

`openroboto check` also reads `model.safetensors.index.json` and verifies every
shard it names is present. **The subnet does not do this** — its rule book performs
no file I/O. A broken index does not get you rejected after burning; it gets you
*admitted* after burning, and then the evaluator cannot load the model. This
command is the only place it is caught before the money moves.

Also worth running before anything costs money:

```bash
openroboto doctor
```

It names every missing config field, checks your HF permissions and your wallet
balance **against this season's own fee**, and tells you whether the training image
for this season is actually present rather than assuming it. It runs offline.

---

## 7. Upload → pay → announce

**This is the part that has not changed.** If you have submitted to the π0.5
competition, everything below should look exactly like what you already do.

```bash
openroboto submit
```

One command, three steps, resumable:

1. **Upload** — pushes your checkpoint directory verbatim as your HuggingFace
   repository root, to `{username}/{base_model_family}-{last 12 of hotkey}`: one
   repository per **season**, shared by every round *within* this season.
   `upload_folder` never deletes, so this round is laid on top of the last one.
   Keeping an existing repository instead? Set `huggingface.repo_id`.
2. **Pay** — **0.1 TAO**, and this season's `fee.kind` is `burn`, so it is
   destroyed rather than transferred. The amount comes from the competition and is
   confirmed against the backend in the moment before it is paid. It is not
   refundable.
3. **Announce** — publishes the chain commitment and waits for it to be included
   in a block.

Two gates run between the upload and the payment, and neither has a flag to skip
it — `--force` does not skip them either:

- **the layout gate**, which judges the *file listing of your HuggingFace
  repository* at the commit you just pushed — not your local directory. Those are
  not the same files: a `.cache/` left behind by an earlier round is
  `LEFTOVER_UPLOAD_STATE` to the subnet and invisible to any check that only looks
  at this round's output;
- **the competition check**, which prints which competition the fee is for, how
  long until submissions close, the amount, how it is collected and to whom, and
  only then asks you to confirm. ⚠️ It does **not** compare `(base_repo,
  base_revision)` — that gate was removed in 1.1.1, see §1.

If HuggingFace is unreachable and the listing cannot be read, it **refuses to
pay**. Stopping there costs you one command — the upload is already recorded, and
re-running `submit` re-transmits nothing.

Recovering a specific round is the same command with the round named:

```bash
openroboto submit --round 1
```

> **⚠️ There is no way to split payment and announcement, and that is the point.**
> The subnet rejects any submission whose payment is more than **50 blocks
> (~10 minutes)** from the chain commitment, and a rejected submission's TAO is not
> refunded. `openroboto submit` runs the three steps back to back precisely so you
> stay inside that window, and refuses to announce once the window has passed
> rather than charging you a commitment fee for a submission that is already
> doomed. `upload` / `burn` / `announce` stopped being separate commands in 1.0.

Re-running `openroboto submit` after a failure resumes from the last completed
step and **reuses the fee already paid instead of paying twice**.

The full picture — the chain payload fields, the three confirmation outcomes and
what to do about each, resume semantics — is [MINER.md](./MINER.md) §"Chain
Submission Format" onward, and it is accurate for this competition too. What the
fee buys and when it is wasted is [PAYMENT.md](./PAYMENT.md).

---

## 8. See what the subnet made of it

```bash
openroboto status
```

Your submissions, the exact rejection reason if there is one, and your position on
the entry list. No API key needed.

One rough edge today: on a hotkey that already has π0.5-era submissions, the
deployed backend returns rows with fields this client's schema does not accept,
and `status` prints a shape-mismatch error instead of your history. A hotkey with
no legacy rows prints normally. This clears when the backend deploys.

---

## 9. If you are still on a π0.5 workspace

The switch already happened (2026-08-31). An older client cannot say which
competition it is submitting to, so the submission is attributed to whichever
simulation competition is running — the LingBot one. Your π0.5 checkpoint then fails
that competition's format check, the submission is rejected, and **the fee is gone.**

Upgrade the client (`pip install -U openroboto`, 1.2.0 or newer) and re-run
`openroboto init --refresh`.
