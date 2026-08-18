"""通过 Docker 调用 openpi-runner 训练容器。

⚠️ **红线：容器的调用方式一个字不改。** openpi 要 `numpy<2.0`、bittensor 要
`numpy>=2.0`，一个解释器装不下 —— 所以训练必须跑在容器里，宿主进程只负责拼命令。
挂载点、环境变量名、顺序、超时都与旧 `miner/training_pipeline_vla.py` 逐字一致：

    -v <output>:/data/output   -v <数据临时目录>:/data/input
    -e TRAIN_DATA / OUTPUT_DIR / EPOCHS / BATCH_SIZE / LR / LORA_R / LORA_ALPHA / HOTKEY
    自定义策略：-v <脚本目录>:/data/scripts  -e CUSTOM_TRAIN=/data/scripts/<脚本名>

策略脚本靠 volume mount 注入，换训练逻辑不用重建镜像；容器侧的接口固定为
`train(cfg, episodes, policy) -> (metrics, proof)`。

`build_docker_command()` 是纯函数，测试逐字比对它的输出 —— 这是这条红线的守卫。
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CONTAINER_NAME = "openpi-runner"
DEFAULT_IMAGE = "robot-train-openpi:latest"
TRAIN_TIMEOUT_SEC = 7200
RESULT_MARKER = "---RESULT---"
FREE_GPU_MEMORY_RATIO = 0.1
"""显存占用低于总量 10% 视为空闲卡。"""


def runner_image() -> str:
    """训练镜像名。`OPENPI_RUNNER_IMAGE` 可覆盖（矿工自建镜像时要用）。"""
    return os.getenv("OPENPI_RUNNER_IMAGE", DEFAULT_IMAGE)


def build_docker_command(
    *,
    train_data_path: str,
    output_dir: str,
    checkpoint_path: str = "",
    val_data_path: str | None = None,
    epochs: int = 3,
    batch_size: int = 4,
    learning_rate: float = 1e-4,
    lora_r: int = 32,
    lora_alpha: int = 64,
    hotkey: str = "unknown",
    custom_train_script: str | None = None,
    visible_devices: str = "",
    image: str | None = None,
) -> list[str]:
    """拼出 `docker run ...` 的完整参数表。改这里等于改训练环境。"""
    data_dir = os.path.dirname(os.path.abspath(train_data_path))
    train_name = os.path.basename(train_data_path)

    command = [
        "docker", "run", "--name", CONTAINER_NAME,
        "--gpus", "all",
        "-v", f"{output_dir}:/data/output",
        "-v", f"{data_dir}:/data/input",
        "-e", f"TRAIN_DATA=/data/input/{train_name}",
        "-e", "OUTPUT_DIR=/data/output",
        "-e", f"EPOCHS={epochs}",
        "-e", f"BATCH_SIZE={batch_size}",
        "-e", f"LR={learning_rate}",
        "-e", f"LORA_R={lora_r}",
        "-e", f"LORA_ALPHA={lora_alpha}",
        "-e", f"HOTKEY={hotkey}",
    ]  # fmt: skip

    if visible_devices:
        command += ["-e", f"CUDA_VISIBLE_DEVICES={visible_devices}"]

    if checkpoint_path:
        if checkpoint_path.startswith("gs://"):
            # GCS 路径由容器内的 openpi 自己下载。
            command += ["-e", f"CHECKPOINT_PATH={checkpoint_path}"]
        else:
            checkpoint = Path(checkpoint_path)
            command += [
                "-v", f"{checkpoint.parent}:/data/checkpoint",
                "-e", f"CHECKPOINT_PATH=/data/checkpoint/{checkpoint.name}",
            ]  # fmt: skip

    if val_data_path:
        command += ["-e", f"VAL_DATA=/data/input/{os.path.basename(val_data_path)}"]

    if custom_train_script:
        script = Path(custom_train_script).resolve()
        command += [
            "-v", f"{script.parent}:/data/scripts",
            "-e", f"CUSTOM_TRAIN=/data/scripts/{script.name}",
        ]  # fmt: skip

    command.append(image or runner_image())
    return command


def detect_free_gpus() -> str:
    """问 nvidia-smi 哪几张卡是空的，返回逗号分隔的序号；问不出来给空串。

    空串的含义是「不设 CUDA_VISIBLE_DEVICES」＝ 用全部卡，与旧行为一致。
    """
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=index,memory.used,memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("nvidia-smi 不可用：%s", exc)
        return ""

    if result.returncode != 0:
        return ""

    free: list[str] = []
    for line in result.stdout.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3:
            continue
        try:
            index, used, total = int(parts[0]), int(parts[1]), int(parts[2])
        except ValueError:
            continue
        if used < total * FREE_GPU_MEMORY_RATIO:
            free.append(str(index))

    if free:
        logger.info("🎯 检测到空闲 GPU：%s", ",".join(free))
    else:
        logger.warning("⚠️  没有空闲 GPU，使用全部卡")
    return ",".join(free)


def remove_stale_container(name: str = CONTAINER_NAME) -> None:
    """删掉上一轮残留的同名容器，否则 `docker run --name` 直接失败。"""
    try:
        listed = subprocess.run(
            ["docker", "ps", "-a", "--filter", f"name=^{name}$", "--format", "{{.ID}}"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        container_id = listed.stdout.strip()
        if container_id:
            logger.info("清理残留容器 %s", container_id)
            subprocess.run(
                ["docker", "rm", "-f", container_id],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("清理容器失败（可忽略）：%s", exc)


def parse_result(stdout: str, output_dir: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """从容器输出里取 metrics / proof。

    先找 stdout 里的 `---RESULT---` 标记，取不到再退回读输出目录里的
    `metrics.json` / `proof.json` —— 容器日志被截断时靠这条兜底。
    """
    metrics: dict[str, Any] = {}
    proof: dict[str, Any] = {}

    if RESULT_MARKER in stdout:
        blob = stdout.split(RESULT_MARKER, 1)[1].strip()
        try:
            parsed = json.loads(blob)
            metrics = parsed.get("metrics", {})
            proof = parsed.get("proof", {})
        except json.JSONDecodeError:
            logger.warning(
                "容器输出里的 %s 段不是合法 JSON，改读输出目录", RESULT_MARKER
            )

    for name, target in (("metrics.json", "metrics"), ("proof.json", "proof")):
        if (metrics if target == "metrics" else proof):
            continue
        path = Path(output_dir) / name
        if path.is_file():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if target == "metrics":
                metrics = loaded
            else:
                proof = loaded

    return metrics, proof


def run_training(
    *,
    train_samples: list[dict[str, Any]],
    output_dir: str,
    eval_samples: list[dict[str, Any]] | None = None,
    checkpoint_path: str = "",
    epochs: int = 3,
    batch_size: int = 4,
    learning_rate: float = 1e-4,
    lora_r: int = 32,
    lora_alpha: int = 64,
    hotkey: str = "unknown",
    custom_train_script: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """跑一次训练容器，返回 (metrics, proof)。

    Raises:
        TrainingError: 数据为空，或容器非零退出。
    """
    if not train_samples:
        raise TrainingError("训练集为空 —— 检查 control.json 的 dataset.train_url")

    os.makedirs(output_dir, exist_ok=True)
    remove_stale_container()

    with tempfile.TemporaryDirectory() as tmpdir:
        train_json = os.path.join(tmpdir, "train.json")
        with open(train_json, "w", encoding="utf-8") as f:
            json.dump(train_samples, f)

        val_json: str | None = None
        if eval_samples:
            val_json = os.path.join(tmpdir, "val.json")
            with open(val_json, "w", encoding="utf-8") as f:
                json.dump(eval_samples, f)

        command = build_docker_command(
            train_data_path=train_json,
            output_dir=output_dir,
            checkpoint_path=checkpoint_path,
            val_data_path=val_json,
            epochs=epochs,
            batch_size=batch_size,
            learning_rate=learning_rate,
            lora_r=lora_r,
            lora_alpha=lora_alpha,
            hotkey=hotkey,
            custom_train_script=custom_train_script,
            visible_devices=detect_free_gpus(),
        )
        logger.info("🐳 启动 openpi-runner：%s", " ".join(command))

        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=TRAIN_TIMEOUT_SEC,
            check=False,
        )
        metrics, proof = parse_result(completed.stdout, output_dir)

    if completed.returncode != 0:
        raise TrainingError(
            f"训练容器退出码 {completed.returncode}\n"
            f"  stderr: {completed.stderr[:500]}\n"
            f"  → 先跑 `openroboto doctor` 确认 GPU / Docker / 镜像都就位"
        )
    return metrics, proof


class TrainingError(Exception):
    """训练没能跑完。多数是环境问题（镜像缺失、显存不足），报错要指向 doctor。"""
