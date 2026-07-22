"""
openpi_runner.py — Isolated π₀.₅ training container via Docker

Main process only installs bittensor (numpy>=2.0), not openpi.
Training calls openpi-runner image via docker run with mounted data.
"""

import os
import json
import subprocess
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Default image name (overridable via OPENPI_RUNNER_IMAGE env var)
OPENPI_RUNNER_IMAGE = os.getenv("OPENPI_RUNNER_IMAGE", "robot-train-openpi:latest")


def run_openpi_training(
    train_data_path: str,
    output_dir: str,
    checkpoint_path: str = "",
    val_data_path: Optional[str] = None,
    epochs: int = 3,
    batch_size: int = 4,
    learning_rate: float = 1e-4,
    lora_r: int = 32,
    lora_alpha: int = 64,
    hotkey: str = "unknown",
    image: Optional[str] = None,
) -> tuple:
    """
    启动 openpi-runner Docker 容器执行训练任务。

    Launch openpi-runner container for training.

    Args:
        train_data_path: training set JSON path / 训练集 JSON 路径
        output_dir: output dir (adapter/, metrics.json, proof.json) / 输出目录
        checkpoint_path: π₀.₅ checkpoint path (GCS URL or local) / 模型检查点路径
        val_data_path: validation set path (optional) / 验证集路径（可选）
        epochs, batch_size, learning_rate: training hyperparams / 训练超参数
        lora_r, lora_alpha: LoRA parameters / LoRA 微调参数
        hotkey: miner hotkey (written to proof) / 矿工热键
        image: custom image name / 自定义镜像名称

    Returns:
        (metrics_dict, proof_dict) / 包含训练指标和证明的字典
    """
    image = image or OPENPI_RUNNER_IMAGE

    # Ensure output directory exists
    os.makedirs(output_dir, exist_ok=True)

    # Build docker run command
    data_dir = os.path.dirname(os.path.abspath(train_data_path))
    train_name = os.path.basename(train_data_path)

    cmd = [
        "docker", "run", "--rm",
        "--gpus", "all",
        "-v", f"{output_dir}:/data/output",
        "-v", f"{data_dir}:/data/input",
        "-e", f"TRAIN_DATA=/data/input/{train_name}",
        "-e", f"OUTPUT_DIR=/data/output",
        "-e", f"EPOCHS={epochs}",
        "-e", f"BATCH_SIZE={batch_size}",
        "-e", f"LR={learning_rate}",
        "-e", f"LORA_R={lora_r}",
        "-e", f"LORA_ALPHA={lora_alpha}",
        "-e", f"HOTKEY={hotkey}",
    ]

    if checkpoint_path:
        if checkpoint_path.startswith("gs://"):
            # GCS path: auto-download inside container
            cmd.extend(["-e", f"CHECKPOINT_PATH={checkpoint_path}"])
        else:
            cp_dir = os.path.dirname(checkpoint_path)
            cp_name = os.path.basename(checkpoint_path)
            cmd.extend([
                "-v", f"{cp_dir}:/data/checkpoint",
                "-e", f"CHECKPOINT_PATH=/data/checkpoint/{cp_name}",
            ])

    if val_data_path:
        cmd.extend(["-e", f"VAL_DATA=/data/input/{os.path.basename(val_data_path)}"])

    cmd.append(image)

    logger.info(f"🐳 Launching openpi-runner container: {' '.join(cmd[:8])}...")

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=7200,  # 2h timeout
    )

    # Parse output
    metrics = {}
    proof = {}

    # Look for ---RESULT--- marker
    if "---RESULT---" in result.stdout:
        idx = result.stdout.index("---RESULT---")
        json_str = result.stdout[idx + len("---RESULT---"):].strip()
        try:
            data = json.loads(json_str)
            metrics = data.get("metrics", {})
            proof = data.get("proof", {})
        except json.JSONDecodeError:
            logger.warning("Failed to parse training result JSON")

    # If container output has no JSON, try reading from output_dir
    metrics_path = os.path.join(output_dir, "metrics.json")
    proof_path = os.path.join(output_dir, "proof.json")
    if not metrics and os.path.exists(metrics_path):
        with open(metrics_path) as f:
            metrics = json.load(f)
    if not proof and os.path.exists(proof_path):
        with open(proof_path) as f:
            proof = json.load(f)

    if result.returncode != 0:
        logger.error(f"Training container exit code {result.returncode}")
        if result.stderr:
            logger.error(f"stderr: {result.stderr[:500]}")

    return metrics, proof
