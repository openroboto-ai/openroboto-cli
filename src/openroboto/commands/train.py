"""`openroboto train` —— 跑一轮训练（旧 `miner.py` 的 Step 1-2）。

轮次、数据集、超参都来自 control.json；训练本身在 openpi-runner 容器里跑
（红线 #2，见 `training/container.py`）。跑完把断点写进 `state/round_N.json`，
后面的 upload / burn / announce 从那里接着走。
"""

from __future__ import annotations

import argparse
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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
    parser = subparsers.add_parser("train", help="跑一轮训练")
    parser.add_argument("--config", default="miner.yaml")
    parser.add_argument(
        "--output-dir", default=str(DEFAULT_OUTPUT_ROOT), help="训练输出根目录"
    )
    parser.add_argument(
        "-s",
        "--strategy",
        default="",
        help="自定义训练脚本，覆盖 miner.yaml 的 custom_train_script",
    )
    parser.set_defaults(handler=run)


def run(args: argparse.Namespace) -> int:
    settings = Settings.load(args.config)

    if not settings.control_json_url:
        fail("未配置 urls.control_json —— 没有它就不知道现在是第几轮、训哪份数据")
        return 1

    control = fetch_control(settings.control_json_url).control
    if control is None:
        fail("control.json 返回 304 但本地没有缓存，重试一次即可")
        return 1
    apply_control(settings, control)

    round_num = int(control.get("round", 0))
    status = str(control.get("status", ""))
    if status != ACTIVE_STATUS:
        fail(f"当前轮次 {round_num} 状态是 `{status}`，不是 `active`，现在训了也没处交")
        return 1

    say(f"🦞 Round {round_num} | hotkey={settings.hotkey} | HF={settings.hf_username}")

    state = load_state(round_num)
    output_dir = str(Path(args.output_dir) / f"round_{round_num}")

    if is_step_done(state, "training"):
        say(f"⏭️  Round {round_num} 已经训练完成（state/round_{round_num}.json）")
        say(f"    → 下一步 `openroboto check {state.get('round_output', output_dir)}`")
        return 0

    dataset = _section(control, "dataset")
    train_url = dataset.get("train_url") or settings.dataset_train_url
    if not train_url:
        fail(
            "训练集地址为空：control.json 的 dataset.train_url 与 "
            "miner.yaml 的 urls.dataset_train 都没有"
        )
        return 1
    val_url = dataset.get("val_url") or settings.dataset_val_url

    params = TrainParams.from_control(_section(control, "training"))
    checkpoint = resolve_checkpoint(settings.vla_checkpoint_path)
    strategy = args.strategy or settings.custom_train_script
    if strategy and not Path(strategy).is_file():
        fail(
            f"找不到训练脚本 {strategy} —— "
            "检查 miner.yaml 的 custom_train_script 或 -s 参数"
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
                say("⏭️  没有验证集，继续")

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
            "训练结果无效（final_loss 缺失或为 0），多半是容器里 OOM 了。\n"
            "  → 看容器日志，或把 batch_size 调小；"
            f"断点已标 failed：state/round_{round_num}.json"
        )
        return 1

    state["training_metrics"] = outcome.metrics
    state["training_proof"] = outcome.proof
    state["step"] = "training"
    state["status"] = "completed"
    if settings.hotkey_ss58:
        state["hotkey_ss58"] = settings.hotkey_ss58
    save_state(round_num, state)

    say(f"✅ 训练完成，模型在 {output_dir}")
    say("")
    say("⚠️  默认训练产物是 LoRA adapter，直接提交**必被拒**（评测器不做合并）。")
    say("    先把 adapter 合进 π0.5 基座导出完整 checkpoint，然后：")
    say(f"    openroboto check {output_dir}      # 免费，本地判定，别跳过")
    say(f"    openroboto submit --round {round_num}")
    return 0


def _section(control: dict[str, Any], name: str) -> dict[str, Any]:
    value = control.get(name)
    return value if isinstance(value, dict) else {}
