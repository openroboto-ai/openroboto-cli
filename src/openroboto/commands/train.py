"""`openroboto train` -- train once against this workspace's competition.

Everything this command needs is already on disk when it starts: the season's
spec in the `competition:` section of `miner.yaml` (status, image, dataset, base
checkpoint) and the miner's own hyperparameters beside it. **Not one byte is
fetched**, beyond the dataset that season names. Training runs inside the runner
container (red line #2, see `training/container.py`); when it finishes the
checkpoint is written into `state/competition_<id>.json`, and the later upload /
payment / announce continue from there.

Everything that varies per season is read from the competition row, except the
five hyperparameters, which are the miner's: choosing their epoch count and LoRA
rank for them would be deciding the competition on their behalf.

The output directory **is the checkpoint root**
------------------------------------------------
Whatever the strategy leaves in `cfg["output_dir"]` is what `submit` uploads,
byte for byte, as the Hugging Face repository root. Nothing in this package
rearranges it afterwards: there is no `openroboto merge` and the evaluator
merges nothing either.

That makes the layout the strategy writes the whole game, and two ways of
getting it wrong are common enough to be called out at the end of every run
(see `export_advice`): exporting nothing at all, and exporting into a
subdirectory. The second one is not carelessness -- the vendor's own LingBot
export lands in `checkpoints/global_step_N/hf_ckpt/`, three levels down, and
the evaluator stops searching at two.
"""

from __future__ import annotations

import argparse
import tempfile
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openroboto import adapters
from openroboto.commands.build import competition_image
from openroboto.commands.check import weights_subdir
from openroboto.competition import REFRESH_HINT, Snapshot, load_snapshot
from openroboto.competition_state import (
    DEFAULT_OUTPUT_ROOT,
    is_step_done,
    load_state,
    save_state,
)
from openroboto.config import Settings
from openroboto.console import fail, say
from openroboto.training.run import (
    TrainParams,
    download_dataset,
    resolve_checkpoint,
    train_once,
)

ACTIVE_STATUS = "active"


def add_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("train", help="Train once")
    parser.add_argument("--config", default="miner.yaml")
    parser.add_argument(
        "--output-dir",
        default=str(DEFAULT_OUTPUT_ROOT),
        help="root directory for training output",
    )
    parser.add_argument(
        "-s",
        "--strategy",
        default="",
        help="custom training script; overrides custom_train_script in miner.yaml",
    )
    parser.set_defaults(handler=run)


def run(args: argparse.Namespace) -> int:
    settings = Settings.load(args.config)

    adapter = adapters.resolve(settings.competition_adapter)
    if adapter.training == adapters.UNAVAILABLE:
        # Refuse before anything is fetched or created. A no-op "run" would
        # leave an empty output directory behind, and the next `openroboto
        # check` would deliver a verdict about it -- a verdict about nothing.
        fail(
            f"Training support for this competition (adapter "
            f"`{settings.competition_adapter}`) has not been released yet: the "
            f"one training image that ships with this client installs openpi "
            f"(π0.5), which is not the base model this competition is judged "
            f"on.\n"
            f"   🔴 It will not quietly run that image instead. An image named "
            f"after this competition may already be on this machine -- built by "
            f"an older release out of the openpi context -- and training in it "
            f"would finish with no error at all, on the wrong base model.\n"
            f"   → train it however you like, then `openroboto check` and "
            f"`openroboto submit` -- both work on a checkpoint this CLI did not "
            f"produce\n"
            f"   → watch for the announcement, then `pip install -U openroboto`"
        )
        return 1

    snapshot = load_snapshot(settings)
    if snapshot is None:
        fail(
            "This workspace does not say which competition it mines, so there "
            "is nothing to train against, no dataset to train on and no base "
            "model to start from.\n"
            "   → `openroboto init --refresh` writes the `competition:` section "
            "from the backend"
        )
        return 1

    if snapshot.status != ACTIVE_STATUS:
        fail(
            f"{snapshot.name} ({snapshot.label}) is `{snapshot.status}`, not "
            f"`active` — anything you train against it has nowhere to be "
            f"submitted.\n" + REFRESH_HINT
        )
        return 1

    competition_id = snapshot.id
    say(
        f"🦞 {snapshot.label} ({snapshot.name}) | hotkey={settings.hotkey} | "
        f"HF={settings.hf_username}"
    )

    state = load_state(competition_id)
    output_dir = str(Path(args.output_dir) / f"competition_{competition_id}")

    if is_step_done(state, "training"):
        say(
            f"⏭️  {snapshot.name} is already trained "
            f"(state/competition_{competition_id}.json)"
        )
        say(f"    → next: `openroboto check {state.get('output_dir', output_dir)}`")
        return 0

    dataset = _dataset(snapshot)
    train_url = str(dataset.get("train") or "")
    if not train_url:
        fail(
            f"{snapshot.name} ({snapshot.label}) has not published a training "
            f"set (`competition.params.training.dataset`), so there is nothing "
            f"to train on.\n"
            f"   Refusing rather than reaching for another season's data: a run "
            f"on the wrong dataset finishes without an error and is only found "
            f"out after the fee is paid.\n" + REFRESH_HINT
        )
        return 1
    val_url = str(dataset.get("val") or "")

    params = TrainParams(
        epochs=settings.epochs,
        batch_size=settings.batch_size,
        learning_rate=settings.learning_rate,
        lora_r=settings.lora_r,
        lora_alpha=settings.lora_alpha,
    )
    # The season's starting point wins over the local path, exactly as
    # control.json's `training.vla_checkpoint_path` did before it. Empty on both
    # sides is a real answer -- "this season does not name one" -- and it leaves
    # the training image to use its own base, which is the only thing that knows
    # what its base is.
    checkpoint = resolve_checkpoint(
        str(snapshot.training.get("checkpoint") or "") or settings.vla_checkpoint_path
    )
    strategy = args.strategy or settings.custom_train_script
    if strategy and not Path(strategy).is_file():
        fail(
            f"training script {strategy} not found — "
            "check custom_train_script in miner.yaml or the -s argument"
        )
        return 1

    state.update(
        {
            "competition_seq": snapshot.seq,
            "step": "prep",
            "status": "completed",
            "started_at": datetime.now(UTC).isoformat(),
            "checkpoint_path": checkpoint,
            "output_dir": output_dir,
            "data_version": f"v{snapshot.seq}",
            "epochs": params.epochs,
            "batch_size": params.batch_size,
            "lr": params.learning_rate,
            "lora_r": params.lora_r,
            "lora_alpha": params.lora_alpha,
        }
    )
    save_state(competition_id, state)

    state["step"] = "training"
    state["status"] = "in_progress"
    save_state(competition_id, state)

    with tempfile.TemporaryDirectory() as tmpdir:
        train_path = download_dataset(train_url, str(Path(tmpdir) / "train.json"))
        val_path: str | None = None
        if val_url:
            try:
                val_path = download_dataset(val_url, str(Path(tmpdir) / "val.json"))
            except OSError:
                say("⏭️  No validation set, continuing")

        outcome = train_once(
            train_json_path=train_path,
            val_json_path=val_path,
            output_dir=output_dir,
            checkpoint_path=checkpoint,
            params=params,
            hotkey=settings.hotkey_ss58 or settings.hotkey,
            custom_train_script=strategy or None,
            # The same image `openroboto build` builds. Resolved from the
            # competition rather than fixed here, so that building one image and
            # training in another is not a thing that can happen quietly.
            image=competition_image(args.config),
            # 🔴 **The season's addresses, from the season's own row.**
            #
            # These used to live only as constants inside the LingBot image
            # (`runner/lingbot/train_runner.py`), which meant changing a base
            # model required a CLI release and every miner rebuilding -- while
            # π0.5 could do the same thing by editing one field. Two seasons on
            # the same client behaving differently is the shape this closes.
            #
            # ⚠️ Empty is meaningful and common: the season names nothing, the
            #    image falls back to the base it was built around, and the
            #    behaviour is byte-for-byte what it was before this existed.
            #    That is what keeps older workspaces working.
            base_weights=str(snapshot.training.get("base_weights") or ""),
            processor=str(snapshot.training.get("processor") or ""),
        )

    if not outcome.metrics.get("final_loss"):
        state["status"] = "failed"
        state["error"] = "training_result_invalid"
        save_state(competition_id, state)
        fail(
            "training result is invalid (final_loss missing or 0), most likely an "
            "OOM inside the container.\n"
            "  → check the container logs, or lower batch_size; "
            f"this attempt is marked failed: "
            f"state/competition_{competition_id}.json"
        )
        return 1

    state["training_metrics"] = outcome.metrics
    state["training_proof"] = outcome.proof
    state["step"] = "training"
    state["status"] = "completed"
    if settings.hotkey_ss58:
        state["hotkey_ss58"] = settings.hotkey_ss58
    save_state(competition_id, state)

    say(f"✅ Training finished, output is in {output_dir}")
    say("")
    for line in export_advice(Path(output_dir)):
        say(line)
    return 0


def export_advice(output_dir: Path) -> list[str]:
    """What the run actually produced, and the next command that is true for it.

    This used to be four fixed lines telling every miner to "merge the adapter
    into the π0.5 base" -- wrong for the LingBot competitions, which do not use
    LoRA at all, and wrong since the merge decision: nothing merges, on this side
    or the evaluator's, and the export is the trainer's job.

    Fixed text cannot be right for all three outcomes anyway, and the difference
    between them is one `rglob` away at the moment the artifact appears. Saying
    it here rather than leaving it to `check` is the point: the nesting case is
    the one that costs money, and a miner who skips `check` meets it after the
    burn.

    The verdict itself still belongs to `check` (red line #1 -- the format rules
    live in the protocol package); every branch ends by pointing at it.
    """
    subdir = weights_subdir(output_dir)

    if subdir is None:
        return [
            "⚠️  There are no model weights in the output.",
            "    Exporting the checkpoint is the training side's job, and the bundled",
            "    strategies do not do it -- they exercise the pipeline, they do "
            "not train.",
            "    Point your trainer's export at the output directory itself: it is",
            "    the checkpoint root, and `submit` uploads it verbatim as the HF",
            "    repository root.",
            "    A bare LoRA adapter is not a substitute: there is no `openroboto "
            "merge`,",
            "    and the evaluator merges nothing either.",
            f"    → openroboto check {output_dir}   # free, local; it names what "
            "is missing",
        ]

    if subdir:
        nested = output_dir / subdir
        return [
            f"⚠️  The checkpoint is in {subdir}/, not at the top of the output.",
            "    `submit` uploads the output directory verbatim as the repository",
            "    root, and the evaluator only searches a couple of levels below it.",
            "    The official LingBot export lands in "
            "checkpoints/global_step_N/hf_ckpt/,",
            "    which is already too deep -- so this is the normal way to get here.",
            "    Submit that directory instead, or move its contents to the top:",
            f"      openroboto check {nested}",
            f"      openroboto submit --output-dir {nested}",
        ]

    return [
        f"    openroboto check {output_dir}      # free, local, do not skip it",
        "    openroboto submit",
    ]


def _dataset(snapshot: Snapshot) -> Mapping[str, Any]:
    """`params.training.dataset`, or `{}` when this season names none.

    `null` is what the backend serves for a season whose dataset has not been
    published, and it must stay distinguishable from a URL — `{}` here becomes
    the refusal above, never a default address.
    """
    value = snapshot.training.get("dataset")
    return value if isinstance(value, Mapping) else {}
