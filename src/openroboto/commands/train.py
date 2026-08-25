"""`openroboto train` -- run one round of training (Step 1-2 of the old
`miner.py`).

The round, the dataset and the hyperparameters all come from control.json;
training itself runs inside the openpi-runner container (red line #2, see
`training/container.py`). When it finishes, the checkpoint is written into
`state/round_N.json`, and the later upload / burn / announce continue from
there.

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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openroboto.commands.check import weights_subdir
from openroboto.config import Settings, apply_control, fetch_control
from openroboto.console import fail, say
from openroboto.round_state import (
    DEFAULT_OUTPUT_ROOT,
    is_step_done,
    load_state,
    save_state,
)
from openroboto.training.round import (
    TrainParams,
    download_dataset,
    resolve_checkpoint,
    train_round,
)

ACTIVE_STATUS = "active"


def add_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("train", help="Run one round of training")
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

    if not settings.control_json_url:
        fail(
            "urls.control_json is not set — without it there is no way to know "
            "which round it is or which dataset to train on"
        )
        return 1

    control = fetch_control(settings.control_json_url).control
    if control is None:
        fail("control.json returned 304 but there is no local cache — just retry")
        return 1
    apply_control(settings, control)

    round_num = int(control.get("round", 0))
    status = str(control.get("status", ""))
    if status != ACTIVE_STATUS:
        fail(
            f"round {round_num} is `{status}`, not `active` — anything you train "
            "now has nowhere to be submitted"
        )
        return 1

    say(f"🦞 Round {round_num} | hotkey={settings.hotkey} | HF={settings.hf_username}")

    state = load_state(round_num)
    output_dir = str(Path(args.output_dir) / f"round_{round_num}")

    if is_step_done(state, "training"):
        say(f"⏭️  Round {round_num} is already trained (state/round_{round_num}.json)")
        say(f"    → next: `openroboto check {state.get('round_output', output_dir)}`")
        return 0

    dataset = _section(control, "dataset")
    train_url = dataset.get("train_url") or settings.dataset_train_url
    if not train_url:
        fail(
            "no training set URL: neither dataset.train_url in control.json nor "
            "urls.dataset_train in miner.yaml is set"
        )
        return 1
    val_url = dataset.get("val_url") or settings.dataset_val_url

    params = TrainParams.from_control(_section(control, "training"))
    checkpoint = resolve_checkpoint(settings.vla_checkpoint_path)
    strategy = args.strategy or settings.custom_train_script
    if strategy and not Path(strategy).is_file():
        fail(
            f"training script {strategy} not found — "
            "check custom_train_script in miner.yaml or the -s argument"
        )
        return 1

    state.update(
        {
            "round": round_num,
            "step": "prep",
            "status": "completed",
            "started_at": datetime.now(UTC).isoformat(),
            "checkpoint_path": checkpoint,
            "round_output": output_dir,
            "data_version": dataset.get("version", f"v{round_num}"),
            "epochs": params.epochs,
            "batch_size": params.batch_size,
            "lr": params.learning_rate,
            "lora_r": params.lora_r,
            "lora_alpha": params.lora_alpha,
        }
    )
    save_state(round_num, state)

    state["step"] = "training"
    state["status"] = "in_progress"
    save_state(round_num, state)

    with tempfile.TemporaryDirectory() as tmpdir:
        train_path = download_dataset(train_url, str(Path(tmpdir) / "train.json"))
        val_path: str | None = None
        if val_url:
            try:
                val_path = download_dataset(val_url, str(Path(tmpdir) / "val.json"))
            except OSError:
                say("⏭️  No validation set, continuing")

        outcome = train_round(
            train_json_path=train_path,
            val_json_path=val_path,
            output_dir=output_dir,
            checkpoint_path=checkpoint,
            params=params,
            hotkey=settings.hotkey_ss58 or settings.hotkey,
            custom_train_script=strategy or None,
        )

    if not outcome.metrics.get("final_loss"):
        state["status"] = "failed"
        state["error"] = "training_result_invalid"
        save_state(round_num, state)
        fail(
            "training result is invalid (final_loss missing or 0), most likely an "
            "OOM inside the container.\n"
            "  → check the container logs, or lower batch_size; "
            f"the round is marked failed: state/round_{round_num}.json"
        )
        return 1

    state["training_metrics"] = outcome.metrics
    state["training_proof"] = outcome.proof
    state["step"] = "training"
    state["status"] = "completed"
    if settings.hotkey_ss58:
        state["hotkey_ss58"] = settings.hotkey_ss58
    save_state(round_num, state)

    say(f"✅ Training finished, output is in {output_dir}")
    say("")
    for line in export_advice(Path(output_dir), round_num):
        say(line)
    return 0


def export_advice(output_dir: Path, round_num: int) -> list[str]:
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
            f"      openroboto submit --round {round_num} --output-dir {nested}",
        ]

    return [
        f"    openroboto check {output_dir}      # free, local, do not skip it",
        f"    openroboto submit --round {round_num}",
    ]


def _section(control: dict[str, Any], name: str) -> dict[str, Any]:
    value = control.get(name)
    return value if isinstance(value, dict) else {}
