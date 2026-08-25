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
| `python rt.py upload --config miner.yaml --round 1` | `openroboto upload --round 1` |
| `python rt.py burn --config miner.yaml` | `openroboto burn` |
| `python rt.py announce --config miner.yaml --round 1` | `openroboto announce --round 1` |
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

**Your existing `miner.yaml` still works.** No field was renamed — that was a hard
constraint, because renaming a key silently breaks every miner's file.

Two things to know:

1. **The flat `[DEFAULT]` / `key = value` form is not supported** and never really
   was by this parser. It fails *quietly*: the file loads, every field falls back to a
   default, and the first symptom is an unrelated complaint about a missing `netuid`.
   If your file looks like that, run `openroboto init` into a scratch directory and
   copy your values into the nested layout.

2. **`payment.burn_rate_tao` is optional and normally should be absent.** The fee comes
   from the subnet's `control.json`. If you hard-coded it, delete it — a stale local
   value is how you burn the wrong amount, and a wrong amount is rejected with no
   refund.

Run this after any edit:

```bash
openroboto doctor      # names every field that is missing or unusable
```

## If you are mid-round right now

`state/round_N.json` is unchanged and still read. Finish the round with the new
commands:

- **Trained but not submitted** → `openroboto check`, then `openroboto submit`.
- **Burned but not announced** → `openroboto announce --round N`. It reuses the burn
  recorded in your state file; **do not burn again**. Note the burn→commitment window
  is 50 blocks (~10 minutes) — if more time has passed, `announce` will tell you the
  burn has expired rather than charging you another fee for a submission that would be
  rejected.
- **Announced** → `openroboto status`.

## Behaviour that changed on purpose

These are not renames; the CLI now does something different, and in each case the old
behaviour could cost you TAO:

| | Old | Now |
|---|---|---|
| Fee cannot be fetched | Fell back to a built-in `0.01`, while the network publishes `0.1` — you burned a tenth of the fee and were rejected | **Refuses to burn.** No built-in amount to fall back to |
| Burn is too old to attach | Announced anyway; the backend rejected it and the fee was gone | `announce` **refuses**, and says how many blocks have passed |
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

> **Status**: ⛔ **PRE-RELEASE DRAFT — do not announce this section yet.**
> **Updated**: 2026-08-25 · **Audience**: every miner on the simulation competition.
>
> Every `<TBD: …>` below is a value that does not exist yet. Before this section is
> published, all of them must be filled in and this banner replaced with
> `**Status**: current`. The release it describes is not out: the client version, the
> switch date and the protocol package version are all still unset. **Do not treat
> any number in this section as a commitment until then.**
>
> Blocking the release: `openroboto-protocol 0.7.0` (unreleased) · the CLI release
> that pins it · the LingBot training image · the competition list endpoint.

## The three things you actually need to know

| Question | Answer |
|---|---|
| **Do I have to change anything?** | Yes. New CLI version, and you have to **retrain from a new base model**. Nothing you have trained so far carries over. |
| **By when?** | `<TBD: switch date/time UTC>`. Until then the π0.5 competition keeps running normally. |
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
openroboto --version          # expect: openroboto <TBD: version> (openroboto-protocol <TBD: 0.7.0>)

cd my-miner
openroboto init --refresh     # re-fetch the competition spec into miner.yaml;
                              # keeps your wallet settings and HF token
openroboto build              # the training image changed — rebuild it
openroboto train              # retrain from the new base
openroboto check              # must pass before you pay. Free, local, no GPU
openroboto submit
```

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

Upload the new checkpoint as a new revision (or a new repository, your choice) — the
chain announcement points at the exact revision, so the two never get confused.

## Uploading what training produced — read this before you pay

Two things about the LingBot training scripts cost money if you find them out late.
Neither is a bug in your setup; both are defaults, and both are cheap to fix **before**
the burn.

### 1. By default the run writes no HuggingFace weights at all

The official templates set `ckpt_manager: dcp` — PyTorch **distributed checkpoint**, a
sharded format meant for resuming training, not for loading a model. The HF-format
export is a separate switch, `save_hf_weights`, and it appears in **neither** official
config template; the exporter opens with

```python
if not args.train.save_hf_weights:
    return
```

so leaving it out means the export never runs. Put it in your training config:

```yaml
train:
  save_hf_weights: true
```

The conversion is asynchronous and best-effort: the training loop does not wait for it
and does not fail if it breaks. Failures are appended to `async_hf_failures.jsonl` in
the output directory — **check that the file is absent or empty** before you upload,
and check that the shards and `model.safetensors.index.json` are really there. A run
that finished cleanly can still have produced no usable weights.

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

- **Every command name and flag.** `init` / `doctor` / `build` / `train` / `check` /
  `upload` / `burn` / `announce` / `submit` / `status` all keep working the same way.
- **Your `miner.yaml` field names.** A competition section is added; nothing is
  renamed. A file without that section keeps working and is treated as the simulation
  competition, exactly as before.
- **The burn → announce window** is still 50 blocks (~10 minutes). Do not split the
  two steps unless you are recovering.
- **The custom-strategy contract** `train(cfg, episodes, policy) -> (metrics, proof)`
  is unchanged — your own training script keeps its shape.
- **There is still no `openroboto merge`, and there will not be one.** A bare LoRA
  adapter is rejected; exporting a full merged checkpoint is part of training, and
  `openroboto check` catches an unmerged upload before you pay.

## New: the CLI tells you what you are paying for, before it pays

`<TBD: confirm this shipped in the released version before publishing>`

Right before spending anything, `submit` verifies against the backend that the
competition in your config is still the one running, that its submission window is
still open, and that the fee and recipient still match your config. It prints the
competition name, its id, **how long is left before submissions close**, the fee and
the recipient — and only then continues.

If any of that does not line up, or the backend cannot be reached, **it refuses to
pay**. There is no flag to skip it: a burn cannot be undone, and a rejected submission
is not refunded.

## If you keep running the old client after the switch

An older client cannot say which competition it is submitting to, so the submission is
attributed to whichever simulation competition is running at that moment — the LingBot
one. Your π0.5 checkpoint then fails that competition's format check, the submission is
rejected, and **the fee is gone**. This is the single most expensive way to ignore this
page.

## Timetable

| What | When |
|---|---|
| Announcement with the final numbers | `<TBD>` |
| New client on PyPI | `<TBD>` |
| Maintenance window, switch performed | `<TBD>` |
| π0.5 competition closes to new submissions | `<TBD>` |
| First round of the LingBot competition opens | `<TBD>` |
