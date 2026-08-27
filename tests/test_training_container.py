"""Red-line guard: how the training container is invoked.

This file exists to compare the `docker run` argument list **word for word**. The numpy
version conflict between openpi and bittensor means training can only run in a
container, so the mount points and environment variable names are the only interface
between host and container; change one character and the miner's training either cannot
read the data or cannot write out the model.

The expected values come from the old
`miner/training_pipeline_vla.py::_run_openpi_docker`.
"""

from __future__ import annotations

import itertools
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
    """A `gs://` path cannot be mounted; it must be passed to the container verbatim so
    openpi downloads it itself."""
    command = build_docker_command(
        train_data_path="/tmp/data/train.json",
        output_dir="/out",
        checkpoint_path="gs://openpi-assets/checkpoints/pi05_base",
        image=IMAGE,
    )
    assert "-e" in command
    assert "CHECKPOINT_PATH=gs://openpi-assets/checkpoints/pi05_base" in command
    assert not any(part.endswith(":/data/cache") for part in command)


def test_local_checkpoint_is_mounted_where_openpi_downloads_to() -> None:
    """🔴 The mount point has to be `OPENPI_DATA_HOME`, or the cache is a lie.

    The Dockerfile sets `OPENPI_DATA_HOME=/data/cache`, which is where openpi
    writes anything it downloads. This used to mount at `/data/checkpoint` --
    a path nothing else in the image knows -- so the base model landed in the
    container's own writable layer and vanished with the container. The host
    cache stayed empty forever, `training/round.py`'s "cache hit" branch could
    never run, and every `train` re-downloaded several GB in silence.

    Still mounted by **parent** directory: the name has to survive so that
    `CHECKPOINT_PATH` points at the checkpoint and not at its container.
    """
    command = build_docker_command(
        train_data_path="/tmp/data/train.json",
        output_dir="/out",
        checkpoint_path="/models/pi05_base",
        image=IMAGE,
    )
    assert "/models:/data/cache" in command
    assert "CHECKPOINT_PATH=/data/cache/pi05_base" in command


def test_custom_strategy_uses_volume_mount_and_env(tmp_path: Path) -> None:
    """The strategy script is injected via volume mount, so changing the training logic
    does not require rebuilding the image -- red line #2."""
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
    """Miners running their own image override it with OPENPI_RUNNER_IMAGE; this must
    not be dropped."""
    monkeypatch.delenv("OPENPI_RUNNER_IMAGE", raising=False)
    assert runner_image() == DEFAULT_IMAGE

    monkeypatch.setenv("OPENPI_RUNNER_IMAGE", "my/openpi:dev")
    assert runner_image() == "my/openpi:dev"
    assert (
        build_docker_command(train_data_path="/tmp/data/train.json", output_dir="/out")[
            -1
        ]
        == "my/openpi:dev"
    )


def test_parse_result_prefers_stdout_marker(tmp_path: Path) -> None:
    stdout = "training log...\n---RESULT---\n" + json.dumps(
        {"metrics": {"final_loss": 0.5}, "proof": {"total_steps": 10}}
    )
    metrics, proof = parse_result(stdout, str(tmp_path))
    assert metrics == {"final_loss": 0.5}
    assert proof == {"total_steps": 10}


def test_parse_result_falls_back_to_output_files(tmp_path: Path) -> None:
    """When the container log is truncated, the files in the output directory are the
    only source of results."""
    (tmp_path / "metrics.json").write_text('{"final_loss": 0.25}', encoding="utf-8")
    (tmp_path / "proof.json").write_text('{"total_steps": 7}', encoding="utf-8")

    metrics, proof = parse_result("a log with no marker", str(tmp_path))
    assert metrics == {"final_loss": 0.25}
    assert proof == {"total_steps": 7}


def test_parse_result_survives_broken_json(tmp_path: Path) -> None:
    metrics, proof = parse_result("---RESULT---\n{不是 JSON", str(tmp_path))
    assert metrics == {}
    assert proof == {}


def test_every_bind_mount_source_is_an_absolute_path(tmp_path: Path) -> None:
    """docker does not treat a relative path as a host directory, and both failure modes
    are expensive.

    - source containing a slash (`tmp/robot_train_vla_miner/round_1`) -> the daemon
      **refuses to start the container**:
      `includes invalid characters for a local volume name`.
    - source without a slash (`cache`) -> **silently** treated as a named volume: the
      container mounts an empty directory, the host directory of the same name is
      neither read nor written, and nothing reports an error.

    This actually happened: `DEFAULT_OUTPUT_ROOT` was written as
    `Path("./tmp/robot_train_vla_miner")`, `Path` normalises the `./` away, `str()`
    comes out as `tmp/...`, and so `openroboto train` with default arguments **could
    not start a container at all**. The base-model cache case is nastier: no error, it
    just re-downloads several GB every round.

    Pin "all absolute" rather than pinning each specific path -- the next mount point
    someone adds gets caught by this one too.
    """
    train = tmp_path / "input" / "train.json"
    train.parent.mkdir(parents=True)
    train.write_text("[]", encoding="utf-8")
    script = tmp_path / "train_strategy.py"
    script.write_text("def train(cfg, episodes, policy=None):\n    return {}, {}\n")

    command = build_docker_command(
        train_data_path="input/train.json",  # relative
        output_dir="tmp/robot_train_vla_miner/round_1",  # relative, with a slash
        checkpoint_path="cache/pi05_base",  # relative, no slash -- silent named volume
        custom_train_script=str(script),
    )

    sources = [
        value.rsplit(":", 1)[0]
        for flag, value in itertools.pairwise(command)
        if flag == "-v"
    ]
    assert sources, (
        "no mounts at all? then this command can neither read data nor write a model"
    )
    for source in sources:
        assert source.startswith("/"), (
            f"mount source is not an absolute path: {source!r}\n"
            f"docker will either treat it as a named volume or refuse to start; "
            f"neither is what you want."
        )
