# Migration

Two migrations live in this file. They are independent — read the one that applies
to you, or both.

| Migration | Who it affects |
|---|---|
| [`python rt.py` → `openroboto`](#1-python-rtpy--openroboto) | Miners who cloned this repository before 2026-08-19 |
| [π0.5 → LingBot base model](#2-π05--lingbot-base-model) | **Every miner.** Your current model does not carry over |

---

# 1. `python rt.py` → `openroboto`

> **Status**: current · **Updated**: 2026-08-19 · **Audience**: miners who cloned this
> repository before 2026-08-19
> **Scope**: what changed, what your old commands map to, and what to do if you are
> mid-round right now.
> **Note**: If you installed with `pip install openroboto` you were never on the old
> path and nothing here affects you.

## What changed

The old entry points are **deleted**, not deprecated:

`rt.py` · `miner.py` · `payment.py` · `validator.py` · `miner/` · `utils/` ·
`protocol/` · `download_checkpoint.py` · `download_checkpoint.sh` ·
`requirements.txt` · `miner.example.yaml` · `validator.example.yaml`

If you `git pull`, `python miner.py` and `python rt.py submit` stop working. There is
no `rt` alias — an abbreviation nobody can read is the naming this repository set out
to remove, and shipping an alias would keep it alive indefinitely.

Everything those files did is in the `openroboto` command, installed from PyPI. You no
longer clone anything.

## Install

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install openroboto
openroboto --version          # CLI version + protocol package version
```

## Command map

| Before | Now |
|---|---|
| `bash download_checkpoint.sh` | *(gone)* — the training container fetches the base checkpoint into `cache/pi05_base` itself |
| `cp miner.example.yaml miner.yaml` | `openroboto init my-miner` — a whole workspace: `miner.yaml` (subnet constants pre-filled), `train_strategy.py`, a `README.md`, and a `.gitignore` that keeps your wallet password out of git |
| `cp validator.example.yaml validator.yaml` | `openroboto init --validator` |
| `docker build -t robot-train-openpi:latest openpi-runner/` | `openroboto build` |
| `python miner.py --config miner.yaml` | `openroboto train` |
| *(nothing — you cloned a second repo)* | `openroboto check` — checkpoint format verdict, locally, **before** you pay |
| `python rt.py submit --config miner.yaml --round 1` | `openroboto submit --round 1` |
| `python rt.py upload --config miner.yaml --round 1` | `openroboto submit` |
| `python rt.py burn --config miner.yaml` | `openroboto submit` |
| `python rt.py announce --config miner.yaml --round 1` | `openroboto submit` |
| *(nothing — you read the website)* | `openroboto status` — your submissions and the exact rejection reason |
| `python validator.py --config validator.yaml` | `openroboto validator run` |
| `docker compose up --build miner` | `openroboto train` — see below |

`--config miner.yaml` is the default everywhere, so you can drop it.

### About `docker compose up --build miner`

That command did two things at once, and only one of them was ever about you.

It built an image containing the **miner code**, and that code then started a
**second** container to do the actual training — openpi needs `numpy<2.0` while
bittensor needs `numpy>=2.0`, so training has always run in its own container.

Now the CLI runs on the host (`pip install openroboto`) and starts the training
container for you. `openroboto train` is the whole replacement:

```bash
openroboto build     # build the training image, once
openroboto train     # runs it, with your data and strategy mounted in
```

The training image definition ships inside the package, so there is nothing to
clone and nothing to keep in sync. You still need Docker on the host — that has
not changed and cannot.

If what you actually wanted was to keep the CLI itself off your host Python,
that is still possible but it is a repository-level thing, not a miner
workflow — see "Running the CLI in a container" in the repository README.

## `miner.yaml` changes

**No field was renamed** — that was a hard constraint, because renaming a key
silently breaks every miner's file. But one section was **added and made
required**, so an untouched file does not submit any more.

Three things to know:

0. 🔴 **You must add a `competition:` section.** `openroboto submit` refuses a file
   without one before it uploads anything: a fee paid with no season attached is
   filed under whichever season the backend defaults to, and it is not refunded.
   `openroboto init --refresh` writes that section from the backend and leaves every
   other line byte for byte as it is (the previous version is kept as
   `miner.yaml.bak`).

1. **The flat `[DEFAULT]` / `key = value` form is not supported** and never really
   was by this parser. It fails *quietly*: the file loads, every field falls back to a
   default, and the first symptom is an unrelated complaint about a missing `netuid`.
   If your file looks like that, run `openroboto init` into a scratch directory and
   copy your values into the nested layout.

2. **`payment.burn_rate_tao` does nothing; delete it.** The fee is the entering
   season's `competition.params.fee.amount_tao`, confirmed against the backend in
   the moment before it is paid. A number here says how much, never which
   competition, so it is not a way to pay — `openroboto submit` refuses a
   workspace that has no `competition` section instead of charging it a
   subnet-wide rate. Run `openroboto init --refresh` to write that section.

Run this after any edit:

```bash
openroboto doctor      # names every field that is missing or unusable
```

## If you are mid-round right now

`state/round_N.json` is unchanged and still read. Finish the round with the new
commands:

- **Trained but not submitted** → `openroboto check`, then `openroboto submit`.
- **Paid but not announced** → `openroboto submit` again. It resumes from your state
  file: the upload is not repeated and the payment recorded there is reused, so **you
  do not pay twice**. Note the payment→commitment window is 50 blocks (~10 minutes) —
  if more time has passed, `submit` tells you the payment has expired rather than
  charging you another fee for a submission that would be rejected.
- **Announced** → `openroboto status`.

## Behaviour that changed on purpose

These are not renames; the CLI now does something different, and in each case the old
behaviour could cost you TAO:

| | Old | Now |
|---|---|---|
| Fee cannot be fetched | Fell back to a built-in `0.01`, while the network publishes `0.1` — you burned a tenth of the fee and were rejected | **Refuses to burn.** No built-in amount to fall back to |
| Payment is too old to attach | Announced anyway; the backend rejected it and the fee was gone | `submit` **refuses to announce**, and says how many blocks have passed |
| Commitment submitted | Printed a block reference even when nothing had confirmed | Waits for inclusion. Prints a block reference **only** when the chain returned one; otherwise says so and tells you to check `openroboto status` before retrying |

## Getting the old files back

Nothing is lost — they are in this repository's git history:

```bash
git log --diff-filter=D --oneline -- rt.py     # find the deleting commit
git show <commit>^:rt.py > rt.py               # recover one file
```

They are unmaintained from 2026-08-19 and will not receive fixes.

---

# 2. π0.5 → LingBot base model

> **Status**: current · **Completed**: 2026-08-31 · **Audience**: every miner on the
> simulation competition.
>
> The switch happened. The π0.5 simulation competition is archived, the LingBot-VLA
> 2.0 one is open and taking submissions, the CLI is released, both training images
> ship in the wheel, and `GET /api/v1/competitions` serves the season list. Nothing on
> this page is waiting on anything.

## The three things you actually need to know

| Question | Answer |
|---|---|
| **Do I have to change anything?** | Yes. CLI **1.2.0 or newer**, and you have to **retrain from a new base model**. Nothing you have trained so far carries over. |
| **By when?** | Done: the switch was performed on 2026-08-31. The π0.5 competition no longer takes submissions. |
| **What if I don't?** | Everything you submit after the switch is **rejected on format**, and the evaluation fee for a rejected submission is **not refunded**. |

## What is changing

The simulation competition swaps its base model: **π0.5 (openpi) → LingBot-VLA 2.0**.
The exam is the same — the same LIBERO task suites in simulation — but the textbook is
different: different training code, different weight files, different checkpoint
layout.

Because scores from the two base models are not comparable, they are not merged:

- The π0.5 competition becomes a **read-only archive**. Its leaderboard stays visible.
- The new competition starts from zero. **The current champion does not carry over.**
- A π0.5 checkpoint submitted to the new competition **fails admission** — the layout
  rules the backend applies come from the competition's base model, and yours will not
  match.

## What you must do

```bash
pip install -U openroboto
openroboto --version          # expect: openroboto 1.2.0 (openroboto-protocol 0.9.0)

cd my-miner
openroboto init --refresh     # re-fetch the competition spec into miner.yaml;
                              # keeps your wallet settings and HF token

openroboto build              # builds the LingBot training image from the
                              # build context inside the package
openroboto train
openroboto check              # must pass before you pay. Free, local, no GPU
openroboto submit
```

> **`openroboto build` and `openroboto train` work for this competition** — this
> paragraph used to say they refuse. `adapters.ADAPTERS["sim_lingbot"]` was flipped
> to `DOCKER` on 2026-08-26, on the evidence of a real run:
> `scripts/verify_lingbot_runner.py` on an A100-SXM4-80GB, all stages green (the
> container builds, the model loads with every parameter filled from the released
> checkpoint, LoRA attaches, merge-and-export writes a flat checkpoint root). The
> package ships two build contexts — `runner/` for π0.5 and `runner/lingbot/` — and
> `runner_context()` picks by `competition.base_model_family`, so the image named by
> this season can only ever be filled with this season's contents.
>
> ⚠️ Measured peak was **12.4 GiB, weights only, before any batch**. The 14–18 GiB
> figure is weights plus activations and is still arithmetic — a 24 GB card has
> 11.6 GiB of headroom for activations, and that number is one miners report back on.
>
> You can still train it however you like and come back: `openroboto check` and
> `openroboto submit` both work on a checkpoint this CLI did not produce. The whole
> flow is written up step by step in [MINER_LINGBOT.md](./MINER_LINGBOT.md).

`init` lists the competitions that are open and writes the one you pick into
`miner.yaml` — base model, training image, format rules, fee and deadline, all in one
snapshot. Every later command reads that file instead of asking the network, so once
`init` has run you can train offline.

There is **no `--track` flag and no new subcommand**: which competition you are
submitting to is a value in your config, not something you type each time.

## Your existing HuggingFace repository

**It has nothing to do with the new competition.** It is not deleted and it is not
migrated — it simply is not evaluated any more. Nothing you upload to it counts
towards the new competition, and there is no conversion path from a π0.5 fine-tune to
a LingBot one. To keep mining you have to train again, from the new base, following
the workflow above.

**Since 1.2.0 the default is a new repository, one per season.** The name is
`{username}/{base_model_family}-{last 12 of your hotkey}`, e.g.
`kyleab/lingbot-vla-2.0-qXgcGfvRk2Xp`; it used to be a fixed `pi05-` prefix and one
repository for a miner's whole career, which meant every repository still said `pi05`
while holding a LingBot model, and `upload_folder` never deletes, so leftovers from an
earlier season became `LEFTOVER_UPLOAD_STATE` at admission — a terminal rejection with
the fee gone.

**To keep the repository you already use, set `huggingface.repo_id` in `miner.yaml`**
to its full `user/name`. It is then used verbatim. Nothing is lost either way —
submissions are located by their on-chain commitment, not by name — but leaving it
empty after upgrading means the next upload creates a new repository and re-pushes
several GB.

## Uploading what training produced — read this before you pay

Two things about the LingBot training scripts cost money if you find them out late.
Neither is a bug in your setup; both are defaults, and both are cheap to fix **before**
the burn.

### 1. The HuggingFace export runs by default — and lands somewhere unusable

> **Corrected 2026-08-26.** This section previously said the export is off by
> default, that you must add `save_hf_weights: true` yourself, and that the
> conversion is "asynchronous and best-effort" with failures logged to
> `async_hf_failures.jsonl`. **All three are wrong**, read off the vendor's
> source at `github.com/Robbyant/lingbot-vla-v2@main`:
> `TrainingArguments.save_hf_weights` defaults to **`True`** and
> `TrainingArguments.async_save_hf_weights` defaults to **`False`**
> (`lingbotvla/utils/arguments.py`). The quoted
> `if not args.train.save_hf_weights: return` is real
> (`tasks/vla/train_lingbotvla.py`) — it is a guard on a flag that is already on.
> The mistake was reading "absent from the config template" as "off", and it sent
> miners to fix a switch that was never the problem. The nesting half of this
> page, §2 below, was and is correct.

The official templates set `ckpt_manager: dcp` — PyTorch **distributed checkpoint**, a
sharded format meant for resuming training, not for loading a model
(`configs/vla/robotwin/robotwin.yaml`, `configs/vla/real_robot/real_robot.yaml`).
Neither template mentions `save_hf_weights`, but its default is `True`, so a run
started from either one **does** write HuggingFace-format weights as well. You do not
have to switch anything on.

What you do have to deal with is *where* they land — `_run_hf_checkpoint` writes to
`os.path.join(checkpoint_path, "hf_ckpt")`
(`lingbotvla/utils/async_hf_checkpoint.py`), and `checkpoint_path` is
`{output_dir}/checkpoints/global_step_N`. That is §2.

**A failed export stops the run; it does not leave a note.** With
`async_save_hf_weights` at its default `False`, `AsyncHFCheckpointSaver.submit()`
takes its synchronous branch and calls the exporter with `best_effort=False`, which
re-raises — the training command fails. `async_hf_failures.jsonl` is written on the
*asynchronous* path, so on the default configuration "the file is absent" tells you
nothing either way. The check that means something is the one that looks at the
artefact:

```bash
openroboto check path/to/checkpoint   # free, local, no GPU
```

It verifies the shards and `model.safetensors.index.json` are really there, and reads
the index to confirm every shard it names exists.

### 2. The official layout is nested too deep to upload as-is

The vendor's own post-trained artifact, `robbyant/lingbot-vla-v2-6b-robotwin`, is laid
out like this — and so is your training output, because the same script wrote both:

```
lingbotvla_cli.yaml
assets/…
checkpoints/global_step_50000/hf_ckpt/
    model-00001-of-00006.safetensors … model-00006-of-00006.safetensors
    model.safetensors.index.json
    config.json  tokenizer.json  vocab.json  …
```

**Do not `git push` that tree as your submission.** The evaluator looks for a
checkpoint at the repository root, one level down and two levels down — and stops.
The weights above are three levels down, so it finds nothing.

This is the expensive failure mode, worse than being rejected: the upload **passes**
admission, your TAO is burned, your queue slot is used, and the run then fails at the
last step with nothing to show for it. Burns are not refunded.

Upload the checkpoint directory itself as the repository root:

```bash
openroboto check  checkpoints/global_step_50000/hf_ckpt      # free, local, no GPU
openroboto submit --output-dir checkpoints/global_step_50000/hf_ckpt
```

or move everything inside `hf_ckpt/` to the top of your output directory and submit
that. Either way, `config.json`, `model.safetensors.index.json` and the shards have to
end up at the top of the repository.

`openroboto check` catches this for you and **exits non-zero**, printing your own
directory in the fix. That is stricter than the subnet's own admission rules, on
purpose: admission answers "does this submission count", `check` answers "will the
money you are about to spend buy you a score".

## What does **not** change

- **The command names that still exist.** `init` / `doctor` / `build` / `train` /
  `check` / `submit` / `status` keep working the same way. (`upload` / `burn` /
  `announce` were removed in 1.0 — see §1 for the map.)
- **Your `miner.yaml` field names.** A competition section is added; nothing is
  renamed. 🔴 **That section is not optional**: `openroboto submit` refuses a file
  without it, before uploading anything, because a fee paid with no season attached is
  filed under whichever season the backend defaults to and is not refunded. Fix it in
  place with `openroboto init --refresh`, which rewrites that section and leaves every
  other line byte for byte as it is.
- **The payment → announce window** is still 50 blocks (~10 minutes); `submit` keeps
  you inside it.
- **The custom-strategy contract** `train(cfg, episodes, policy) -> (metrics, proof)`
  is unchanged — your own training script keeps its shape.
- **There is still no `openroboto merge`, and there will not be one.** A bare LoRA
  adapter is rejected; exporting a full merged checkpoint is part of training, and
  `openroboto check` catches an unmerged upload before you pay.

## Changed: what the bundled training strategies leave behind

`templates/simple`, `templates/example` and the container's default flow used to
write a fabricated LoRA adapter into `<output_dir>/adapter/`. They no longer write
anything under `adapter/`, and they no longer fabricate weights at all — the export
step is marked and left for you, and both `train` and `check` say so plainly.

Nothing breaks that was working: that adapter was never a submittable artifact
(`openroboto check` rejected it as `bare_lora_adapter`, and so did admission), and
nothing in the pipeline read the `adapter/` directory. **If your own
`train_strategy.py` writes into `<output_dir>/adapter/`, move the export up to
`<output_dir>` itself** — that directory is the checkpoint root, uploaded verbatim
as your Hugging Face repository root.

The same rule catches the LingBot exporter's layout: it writes
`checkpoints/global_step_N/hf_ckpt/`, three levels down, and the evaluator searches
two. `openroboto train` now names that directory when it finds the weights there,
and `openroboto check` prints the `--output-dir` you should submit instead.

## New: the CLI tells you what you are paying for, before it pays

Shipped; released 2026-08-31.

Right before spending anything, `submit` verifies against the backend that the
competition in your config is still the one running, that its submission window is
still open, and that the fee and recipient still match your config. It prints the
competition name, its id, **how long is left before submissions close**, the fee and
the recipient — and only then continues.

If any of that does not line up, or the backend cannot be reached, **it refuses to
pay**. There is no flag to skip it: a payment cannot be undone, and a rejected
submission is not refunded.

## If you keep running the old client after the switch

An older client cannot say which competition it is submitting to, so the submission is
attributed to whichever simulation competition is running at that moment — the LingBot
one. Your π0.5 checkpoint then fails that competition's format check, the submission is
rejected, and **the fee is gone**. This is the single most expensive way to ignore this
page.

## Timetable

**Completed 2026-08-31.** The announcement went out, the client is on PyPI, the switch
was performed, the π0.5 competition stopped taking submissions, and the first LingBot
round opened. Nothing on this page is scheduled; it is a record of what happened.

⚠️ **Use 1.2.0 or newer.** 1.1.x still names repositories `pi05-…` regardless of the
season, which is what 1.2.0 fixed.
