"""红线守卫：训练容器的调用方式。

这个文件的存在意义是**逐字比对** `docker run` 的参数表。openpi 与 bittensor
的 numpy 版本冲突让训练只能跑在容器里，挂载点和环境变量名就是宿主与容器之间
唯一的接口；改一个字符，矿工的训练要么读不到数据要么写不出模型。

期望值来自旧 `miner/training_pipeline_vla.py::_run_openpi_docker`。
"""

from __future__ import annotations

import json
from pathlib import Path

from openroboto.training.container import (
    DEFAULT_IMAGE,
    build_docker_command,
    parse_result,
    runner_image,
)

IMAGE = "robot-train-openpi:latest"


def test_minimal_command_matches_legacy_shape() -> None:
    command = build_docker_command(
        train_data_path="/tmp/data/train.json",
        output_dir="/out",
        image=IMAGE,
    )
    assert command == [
        "docker", "run", "--name", "openpi-runner",
        "--gpus", "all",
        "-v", "/out:/data/output",
        "-v", "/tmp/data:/data/input",
        "-e", "TRAIN_DATA=/data/input/train.json",
        "-e", "OUTPUT_DIR=/data/output",
        "-e", "EPOCHS=3",
        "-e", "BATCH_SIZE=4",
        "-e", "LR=0.0001",
        "-e", "LORA_R=32",
        "-e", "LORA_ALPHA=64",
        "-e", "HOTKEY=unknown",
        IMAGE,
    ]  # fmt: skip


def test_gcs_checkpoint_is_passed_as_env_not_mount() -> None:
    """`gs://` 路径不能挂载，必须原样传给容器让 openpi 自己下。"""
    command = build_docker_command(
        train_data_path="/tmp/data/train.json",
        output_dir="/out",
        checkpoint_path="gs://openpi-assets/checkpoints/pi05_base",
        image=IMAGE,
    )
    assert "-e" in command
    assert "CHECKPOINT_PATH=gs://openpi-assets/checkpoints/pi05_base" in command
    assert not any(part.endswith(":/data/checkpoint") for part in command)


def test_local_checkpoint_is_mounted_by_parent_directory() -> None:
    command = build_docker_command(
        train_data_path="/tmp/data/train.json",
        output_dir="/out",
        checkpoint_path="/models/pi05_base",
        image=IMAGE,
    )
    assert "/models:/data/checkpoint" in command
    assert "CHECKPOINT_PATH=/data/checkpoint/pi05_base" in command


def test_custom_strategy_uses_volume_mount_and_env(tmp_path: Path) -> None:
    """策略脚本靠 volume mount 注入，换训练逻辑不用重建镜像 —— 红线 #2。"""
    script = tmp_path / "my_strategy.py"
    script.write_text("def train(cfg, episodes, policy): ...", encoding="utf-8")

    command = build_docker_command(
        train_data_path="/tmp/data/train.json",
        output_dir="/out",
        custom_train_script=str(script),
        image=IMAGE,
    )
    assert f"{tmp_path}:/data/scripts" in command
    assert "CUSTOM_TRAIN=/data/scripts/my_strategy.py" in command


def test_validation_set_and_gpu_selection_are_optional() -> None:
    command = build_docker_command(
        train_data_path="/tmp/data/train.json",
        output_dir="/out",
        val_data_path="/tmp/data/val.json",
        visible_devices="0,1",
        image=IMAGE,
    )
    assert "VAL_DATA=/data/input/val.json" in command
    assert "CUDA_VISIBLE_DEVICES=0,1" in command

    without = build_docker_command(
        train_data_path="/tmp/data/train.json", output_dir="/out", image=IMAGE
    )
    assert not any(part.startswith("CUDA_VISIBLE_DEVICES") for part in without)
    assert not any(part.startswith("VAL_DATA") for part in without)


def test_image_comes_from_environment_override(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """矿工自建镜像时用 OPENPI_RUNNER_IMAGE 覆盖，这条不能丢。"""
    monkeypatch.delenv("OPENPI_RUNNER_IMAGE", raising=False)
    assert runner_image() == DEFAULT_IMAGE

    monkeypatch.setenv("OPENPI_RUNNER_IMAGE", "my/openpi:dev")
    assert runner_image() == "my/openpi:dev"
    assert build_docker_command(
        train_data_path="/tmp/data/train.json", output_dir="/out"
    )[-1] == "my/openpi:dev"


def test_parse_result_prefers_stdout_marker(tmp_path: Path) -> None:
    stdout = "训练日志...\n---RESULT---\n" + json.dumps(
        {"metrics": {"final_loss": 0.5}, "proof": {"total_steps": 10}}
    )
    metrics, proof = parse_result(stdout, str(tmp_path))
    assert metrics == {"final_loss": 0.5}
    assert proof == {"total_steps": 10}


def test_parse_result_falls_back_to_output_files(tmp_path: Path) -> None:
    """容器日志被截断时，输出目录里的文件是唯一的结果来源。"""
    (tmp_path / "metrics.json").write_text('{"final_loss": 0.25}', encoding="utf-8")
    (tmp_path / "proof.json").write_text('{"total_steps": 7}', encoding="utf-8")

    metrics, proof = parse_result("没有标记的日志", str(tmp_path))
    assert metrics == {"final_loss": 0.25}
    assert proof == {"total_steps": 7}


def test_parse_result_survives_broken_json(tmp_path: Path) -> None:
    metrics, proof = parse_result("---RESULT---\n{不是 JSON", str(tmp_path))
    assert metrics == {}
    assert proof == {}
