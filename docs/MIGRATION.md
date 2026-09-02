# Base model: LingBot-VLA 2.0

> **Status**: current · **Audience**: every miner on the simulation competition.
> **Scope**: what the simulation season expects of your checkpoint, and why a
> π0.5 one is refused.

## The three things you actually need to know

| Question | Answer |
|---|---|
| **What do I need?** | CLI **1.2.0 or newer**, and a checkpoint trained from **LingBot-VLA 2.0**. |
| **Does a π0.5 checkpoint work?** | No. The layout rules the backend applies come from the season's base model, and a π0.5 tree does not match. |
| **What does that cost?** | A submission rejected on format is **not refunded**. `openroboto check` applies the same rules locally, for free, before you pay. |

## What the season expects

The simulation competition runs on **LingBot-VLA 2.0**: different training code,
different weight files, different checkpoint layout from π0.5. The exam is the same —
the same LIBERO task suites in simulation — but the textbook is different.

Scores from two base models are not comparable, so each season is scored on its own
and starts from zero.

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

> **`openroboto build` and `openroboto train` work for this competition.**
> `adapters.ADAPTERS["sim_lingbot"]` is `DOCKER` on the evidence of a real run:
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

**The default is one repository per season.** The name is
`{username}/{base_model_family}-{last 12 of your hotkey}`, e.g.
`kyleab/lingbot-vla-2.0-qXgcGfvRk2Xp`. One repository across seasons does not work:
`upload_folder` never deletes, so leftovers from another season become
`LEFTOVER_UPLOAD_STATE` at admission — a terminal rejection with
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

> ⚠️ **Do not add `save_hf_weights: true`** — it is already on, and the export is
> synchronous, not best-effort. The facts below are read off the vendor's
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

## What the bundled training strategies leave behind

`templates/simple`, `templates/example` and the container's default flow write
nothing under `adapter/` and fabricate no weights — the export step is marked and
left for you, and both `train` and `check` say so plainly.

A bare adapter is not a submittable artifact
(`openroboto check` rejected it as `bare_lora_adapter`, and so did admission), and
nothing in the pipeline read the `adapter/` directory. **If your own
`train_strategy.py` writes into `<output_dir>/adapter/`, move the export up to
`<output_dir>` itself** — that directory is the checkpoint root, uploaded verbatim
as your Hugging Face repository root.

The same rule catches the LingBot exporter's layout: it writes
`checkpoints/global_step_N/hf_ckpt/`, three levels down, and the evaluator searches
two. `openroboto train` now names that directory when it finds the weights there,
and `openroboto check` prints the `--output-dir` you should submit instead.

## The CLI tells you what you are paying for, before it pays

Shipped; released 2026-08-31.

Right before spending anything, `submit` verifies against the backend that the
competition in your config is still the one running, that its submission window is
still open, and that the fee and recipient still match your config. It prints the
competition name, its id, **how long is left before submissions close**, the fee and
the recipient — and only then continues.

If any of that does not line up, or the backend cannot be reached, **it refuses to
pay**. There is no flag to skip it: a payment cannot be undone, and a rejected
submission is not refunded.

⚠️ **Use 1.2.0 or newer.** Earlier clients name every repository `pi05-…`
regardless of the season.
