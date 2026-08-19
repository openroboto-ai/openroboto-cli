"""Invoke the openpi-runner training container through Docker.

⚠️ **Red line: the way the container is invoked must not change by one word.**
openpi needs `numpy<2.0` and bittensor needs `numpy>=2.0`; one interpreter cannot
hold both — so training must run inside a container, and the host process only
assembles the command. Mount points, environment variable names, their order, and
the timeout are all verbatim identical to the old
`miner/training_pipeline_vla.py`:

    -v <output>:/data/output   -v <temp data dir>:/data/input
    -e TRAIN_DATA / OUTPUT_DIR / EPOCHS / BATCH_SIZE / LR / LORA_R / LORA_ALPHA / HOTKEY
    custom strategy: -v <script dir>:/data/scripts
                     -e CUSTOM_TRAIN=/data/scripts/<script name>

The strategy script is injected by volume mount, so swapping training logic needs
no image rebuild; the container-side interface is fixed as
`train(cfg, episodes, policy) -> (metrics, proof)`.

`build_docker_command()` is a pure function and the tests compare its output word
for word — that is what guards this red line.
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
"""A card using less than 10% of its total VRAM counts as free."""


def runner_image() -> str:
    """The training image name.

    `OPENPI_RUNNER_IMAGE` can override it (needed when a miner builds their own
    image).
    """
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
    """Assemble the complete argument list for `docker run ...`.

    Changing anything here means changing the training environment.
    """
    data_dir = os.path.dirname(os.path.abspath(train_data_path))
    train_name = os.path.basename(train_data_path)

    # ⚠️ **每一个 `-v` 的源都必须是绝对路径。** docker 对不以 `/` 开头的源不当路径：
    #   - 含斜杠 → 直接拒绝启动容器
    #     （`"tmp/…" includes invalid characters for a local volume name`）
    #   - 不含斜杠 → **静默**当成具名卷，容器看到的是一个空目录，
    #     宿主那个同名目录一个字节都不会被读到或写到
    #
    # 这两条都实测过。默认输出根是 `Path("./tmp/robot_train_vla_miner")`，
    # 而 `Path` 会把 `./` 规范化掉，`str()` 出来就是 `tmp/…` —— 于是
    # `openroboto train` 用默认配置**根本起不来容器**。
    # 基座缓存那条更阴：`cache` 不含斜杠，不报错，只是永远读不到宿主的缓存，
    # 每轮重下几个 GB，而"命中缓存"的日志照常打印。
    output_mount = Path(output_dir).resolve()

    command = [
        "docker", "run", "--name", CONTAINER_NAME,
        "--gpus", "all",
        "-v", f"{output_mount}:/data/output",
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
            # A GCS path is downloaded by the openpi inside the container itself.
            command += ["-e", f"CHECKPOINT_PATH={checkpoint_path}"]
        else:
            checkpoint = Path(checkpoint_path).resolve()
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
    """Ask nvidia-smi which cards are free and return comma-separated indices.

    Returns an empty string if it cannot be determined. An empty string means "do
    not set CUDA_VISIBLE_DEVICES" = use every card, matching the old behaviour.
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
    """Remove a leftover container of the same name from the previous round.

    Otherwise `docker run --name` fails outright.
    """
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
    """Take metrics / proof out of the container output.

    It first looks for the `---RESULT---` marker in stdout, and if that yields
    nothing it falls back to reading `metrics.json` / `proof.json` from the output
    directory — that fallback is what covers a truncated container log.
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
        if metrics if target == "metrics" else proof:
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
    """Run the training container once and return (metrics, proof).

    Raises:
        TrainingError: the data is empty, or the container exited non-zero.
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

        # 这两条不是"防御性编程"，是这个命令最可能遇到的两种环境故障。
        # 不转成 TrainingError 的话它们会一路裸抛到顶层：矿工看到的是
        # `FileNotFoundError: [Errno 2] ... 'docker'` 加二十行 traceback，
        # 而 AGENTS.md §4 要求报错能自助排查。`build` 与 `doctor` 早就这么做了，
        # 唯独真正长时间调 docker 的这条路径漏了。
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=TRAIN_TIMEOUT_SEC,
                check=False,
            )
        except FileNotFoundError as exc:
            raise TrainingError(
                "找不到 docker —— 训练必须跑在容器里：openpi 要 numpy<2.0、"
                "bittensor 要 numpy>=2.0，一个解释器装不下两个。\n"
                "  → 装 Docker：https://get.docker.com\n"
                "  → 装完先 `openroboto doctor`，它会把 GPU / 驱动 / 镜像一起查掉"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            hours = TRAIN_TIMEOUT_SEC // 3600
            raise TrainingError(
                f"训练容器跑了超过 {hours} 小时仍未结束，已中止。\n"
                f"  这通常不是你的策略脚本写错了，而是卡在下载基座或数据上。\n"
                f"  → `docker logs {CONTAINER_NAME}` 看它最后停在哪一步\n"
                f"  → 确认磁盘还有空间（基座 checkpoint 有几个 GB）"
            ) from exc

        metrics, proof = parse_result(completed.stdout, output_dir)

    if completed.returncode != 0:
        raise TrainingError(
            f"训练容器退出码 {completed.returncode}\n"
            f"  stderr: {completed.stderr[:500]}\n"
            f"  → 先跑 `openroboto doctor` 确认 GPU / Docker / 镜像都就位"
        )
    return metrics, proof


class TrainingError(Exception):
    """Training did not run to completion.

    Usually an environment problem (missing image, not enough VRAM), so the error
    message should point at doctor.
    """
