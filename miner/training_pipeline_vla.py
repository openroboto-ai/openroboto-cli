"""
miner/training_pipeline_vla.py — π₀.₅ LIBERO Training Pipeline

Invokes the isolated openpi-runner container via Docker
to avoid numpy version conflicts (openpi: numpy<2.0 vs bittensor: numpy>=2.0).

Interface contract:
    run_training(policy=None, train_dataset, eval_dataset, output_dir, config)
        -> (train_result_dict, trained_policy)
"""

import os
import json
import logging
import subprocess
from typing import Optional

logger = logging.getLogger(__name__)

OPENPI_RUNNER_IMAGE = os.getenv("OPENPI_RUNNER_IMAGE", "robot-train-openpi:latest")


def run_training(
    policy=None,
    train_dataset: list = None,
    eval_dataset: Optional[list] = None,
    output_dir: str = "./output_vla",
    config=None,
    custom_train_script: Optional[str] = None,
    checkpoint_path: Optional[str] = None,
    **kwargs,
) -> tuple:
    """启动 openpi-runner 容器进行训练 / Start the openpi-runner container for training.

    Args:
        policy: Reserved; container loads its own
        train_dataset: List of training samples
        eval_dataset: List of validation samples (optional)
        output_dir: Output directory
        config: Training config
        checkpoint_path: Explicit checkpoint path override

    Returns:
        (train_result_dict, mock_policy)
    """
    logger.debug(f"[run_training] Start | samples={len(train_dataset) if train_dataset else 0} output={output_dir}")
    if not train_dataset:
        logger.error("[run_training] train_dataset is required")
        raise ValueError("train_dataset is required")

    os.makedirs(output_dir, exist_ok=True)

    epochs = getattr(config, "epochs", 3) if config else 3
    batch_size = getattr(config, "batch_size", 4) if config else 4
    learning_rate = getattr(config, "learning_rate", 1e-4) if config else 1e-4
    lora_r = getattr(config, "lora_r", 32) if config else 32
    lora_alpha = getattr(config, "lora_alpha", 64) if config else 64
    # Use SS58 address as hotkey identifier (avoid YAML int parsing issues)
    hotkey_value = getattr(config, "hotkey_ss58", None) or str(getattr(config, "hotkey", "unknown"))
    logger.debug(f"[run_training] params: epochs={epochs} bs={batch_size} lr={learning_rate} lora_r={lora_r}")

    # Checkpoint path — prefer explicit argument, fallback to config
    if not checkpoint_path:
        if config and hasattr(config, "vla_checkpoint_path") and config.vla_checkpoint_path:
            checkpoint_path = config.vla_checkpoint_path
    logger.debug(f"[run_training] checkpoint_path={checkpoint_path or '(default)'}")
    logger.debug(f"[run_training] custom_train_script={custom_train_script or '(none)'}")

    # Write train_dataset to a temp JSON file
    import tempfile
    with tempfile.TemporaryDirectory() as tmpdir:
        train_json = os.path.join(tmpdir, "train.json")
        with open(train_json, "w") as f:
            json.dump(train_dataset, f)
        logger.debug(f"[run_training] Temp training data written: {train_json} ({os.path.getsize(train_json)} bytes)")

        # Validation set
        val_json = None
        if eval_dataset:
            val_json = os.path.join(tmpdir, "val.json")
            with open(val_json, "w") as f:
                json.dump(eval_dataset, f)
            logger.debug(f"[run_training] Temp validation data written: {val_json}")

        logger.debug("[run_training] Calling _run_openpi_docker")
        metrics, proof = _run_openpi_docker(
            train_data_path=train_json,
            val_data_path=val_json,
            output_dir=output_dir,
            checkpoint_path=checkpoint_path or "",
            epochs=epochs,
            batch_size=batch_size,
            learning_rate=learning_rate,
            lora_r=lora_r,
            lora_alpha=lora_alpha,
            hotkey=hotkey_value,
            custom_train_script=custom_train_script,
        )
        logger.debug(f"[run_training] _run_openpi_docker returned | metrics={metrics}")

    # Mock policy object (actual training happens inside container)
    trained_policy = _MockTrainedPolicy(output_dir)
    logger.debug(f"[run_training] Returning _MockTrainedPolicy | output_dir={output_dir}")

    return metrics, trained_policy


def _cleanup_container(name: str) -> None:
    """清理指定名称的残留 Docker 容器 / Remove any existing container with the given name to prevent resource waste."""
    try:
        result = subprocess.run(
            ["docker", "ps", "-a", "--filter", f"name=^{name}$", "--format", "{{.ID}}"],
            capture_output=True, text=True, timeout=10,
        )
        cid = result.stdout.strip()
        if cid:
            logger.debug(f"[_cleanup_container] Found stale container {cid}, removing")
            subprocess.run(["docker", "rm", "-f", cid], capture_output=True, text=True, timeout=30)
            logger.debug(f"[_cleanup_container] Cleaned up {cid}")
    except Exception as e:
        logger.debug(f"[_cleanup_container] Cleanup failed (ignorable): {e}")


def _run_openpi_docker(
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
    custom_train_script: Optional[str] = None,
    visible_devices: str = "",
) -> tuple:
    """通过 Docker 调用 openpi-runner 进行训练 / Invoke openpi-runner via docker run."""
    CONTAINER_NAME = "openpi-runner"
    logger.debug(f"[_run_openpi_docker] Start | image={OPENPI_RUNNER_IMAGE}")
    logger.debug(f"[_run_openpi_docker] train_data={train_data_path} output={output_dir} ckpt={checkpoint_path}")

    # Clean up any leftover container with the same name
    _cleanup_container(CONTAINER_NAME)

    data_dir = os.path.dirname(os.path.abspath(train_data_path))
    train_name = os.path.basename(train_data_path)

    cmd = [
        "docker", "run", "--name", CONTAINER_NAME,
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
    if visible_devices:
        cmd.extend(["-e", f"CUDA_VISIBLE_DEVICES={visible_devices}"])
        logger.info(f"🎯 Using GPU devices: {visible_devices}")
    else:
        # Auto-find free GPU: check nvidia-smi
        try:
            import subprocess as _sub
            res = _sub.run(
                ["nvidia-smi", "--query-gpu=index,memory.used,memory.total",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=10
            )
            if res.returncode == 0:
                free_gpus = []
                for line in res.stdout.strip().split("\n"):
                    parts = [p.strip() for p in line.split(",")]
                    if len(parts) >= 3:
                        idx, used, total = int(parts[0]), int(parts[1]), int(parts[2])
                        if used < total * 0.1:  # <10% used
                            free_gpus.append(str(idx))
                if free_gpus:
                    gpus_str = ",".join(free_gpus)
                    cmd.extend(["-e", f"CUDA_VISIBLE_DEVICES={gpus_str}"])
                    logger.info(f"🎯 Auto-detected free GPUs: {gpus_str}")
                else:
                    logger.warning("⚠️  No free GPU detected, using all GPUs")
        except Exception as e:
            logger.debug(f"[run_training] GPU detection failed, using all GPUs: {e}")

    if checkpoint_path:
        if checkpoint_path.startswith("gs://"):
            cmd.extend(["-e", f"CHECKPOINT_PATH={checkpoint_path}"])
            logger.debug(f"[_run_openpi_docker] GCS checkpoint: {checkpoint_path}")
        else:
            cp_dir = os.path.dirname(checkpoint_path)
            cp_name = os.path.basename(checkpoint_path)
            cmd.extend([
                "-v", f"{cp_dir}:/data/checkpoint",
                "-e", f"CHECKPOINT_PATH=/data/checkpoint/{cp_name}",
            ])
            logger.debug(f"[_run_openpi_docker] Local checkpoint mount: {cp_dir} → /data/checkpoint")

    if val_data_path:
        cmd.extend(["-e", f"VAL_DATA=/data/input/{os.path.basename(val_data_path)}"])
        logger.debug(f"[_run_openpi_docker] Validation: VAL_DATA=/data/input/{os.path.basename(val_data_path)}")

    if custom_train_script:
        cp_abs = os.path.abspath(custom_train_script)
        cp_dir = os.path.dirname(cp_abs)
        cp_name = os.path.basename(cp_abs)
        cmd.extend([
            "-v", f"{cp_dir}:/data/scripts",
            "-e", f"CUSTOM_TRAIN=/data/scripts/{cp_name}",
        ])
        logger.info(f"🔧 Mounting custom script: {cp_abs}")

    cmd.append(OPENPI_RUNNER_IMAGE)

    logger.info(f"🐳 openpi-runner started")
    logger.debug(f"[_run_openpi_docker] docker cmd: {' '.join(cmd)}")

    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=7200,
    )
    logger.debug(f"[_run_openpi_docker] docker exit code={result.returncode}")
    logger.debug(f"[_run_openpi_docker] stdout length={len(result.stdout)} stderr length={len(result.stderr)}")

    # Parse results
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
            logger.debug(f"[_run_openpi_docker] Parsed stdout JSON OK | metrics_keys={list(metrics.keys())}")
        except json.JSONDecodeError:
            logger.warning("[_run_openpi_docker] Failed to parse training result JSON")
            logger.debug(f"[_run_openpi_docker] stdout snippet: {result.stdout[idx:idx+200]}")
    else:
        logger.debug("[_run_openpi_docker] ---RESULT--- marker not found in stdout")

    # Fallback: read from output directory
    metrics_path = os.path.join(output_dir, "metrics.json")
    proof_path = os.path.join(output_dir, "proof.json")
    if not metrics and os.path.exists(metrics_path):
        logger.debug(f"[_run_openpi_docker] Fallback reading {metrics_path}")
        with open(metrics_path) as f:
            metrics = json.load(f)
    if not proof and os.path.exists(proof_path):
        logger.debug(f"[_run_openpi_docker] Fallback reading {proof_path}")
        with open(proof_path) as f:
            proof = json.load(f)

    if result.returncode != 0:
        logger.error(f"Training container exited with code {result.returncode}")
        if result.stderr:
            logger.error(f"stderr: {result.stderr[:500]}")
        logger.debug(f"[_run_openpi_docker] Full stderr:\n{result.stderr}")

    return metrics, proof


class _MockTrainedPolicy:
    """Placeholder policy object (actual training happens inside container)."""

    def __init__(self, output_dir: str):
        """初始化模拟策略对象 / Initialize the mock policy object."""
        self.output_dir = output_dir

    def save_pretrained(self, output_dir: str):
        """从容器输出复制模型文件 / Container already saves the model; just copy here."""
        import shutil
        adapter_dir = os.path.join(self.output_dir, "adapter")
        if os.path.exists(adapter_dir):
            for item in os.listdir(adapter_dir):
                src = os.path.join(adapter_dir, item)
                dst = os.path.join(output_dir, item)
                if os.path.isdir(src):
                    shutil.copytree(src, dst, dirs_exist_ok=True)
                else:
                    shutil.copy2(src, dst)
