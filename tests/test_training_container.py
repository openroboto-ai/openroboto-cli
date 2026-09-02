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

import pytest

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
    cache stayed empty forever, `training/run.py`'s "cache hit" branch could
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
    just re-downloads several GB every run.

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


# ─────────────────────────────────────────────────────────────────────────────
# The season's addresses reach the container
# ─────────────────────────────────────────────────────────────────────────────


def test_the_seasons_addresses_are_passed_in() -> None:
    """🔴 **Changing a base model must not need a CLI release.**

    Until 2026-08-31 LingBot's weights, revision and processor lived only as
    constants inside the image, so moving to a new base meant cutting a release
    and having every miner rebuild -- while a π0.5 season did the same thing by
    editing one field on its row. Two seasons on the same client behaving
    differently is what this closes.
    """
    command = build_docker_command(
        train_data_path="/tmp/x/train.json",
        output_dir="/tmp/out",
        base_weights="robbyant/lingbot-vla-v2-6b@11c703bf",
        processor="Qwen/Qwen3-VL-4B-Instruct@ebb281ec",
    )
    assert "BASE_WEIGHTS=robbyant/lingbot-vla-v2-6b@11c703bf" in command
    assert "PROCESSOR=Qwen/Qwen3-VL-4B-Instruct@ebb281ec" in command


def test_a_season_that_names_no_address_changes_nothing() -> None:
    """⚠️ **Empty is the normal case, not an edge case.**

    Every workspace written before these fields existed sends nothing, and for
    them the command has to come out byte-for-byte as it did — the image then
    falls back to the base it was built around. A stray `-e BASE_WEIGHTS=`
    would reach the container as an empty string and be read as an address.
    """
    command = build_docker_command(
        train_data_path="/tmp/x/train.json", output_dir="/tmp/out"
    )
    assert not [part for part in command if part.startswith("BASE_WEIGHTS")]
    assert not [part for part in command if part.startswith("PROCESSOR")]


def test_the_address_stays_one_string() -> None:
    """`repo@revision` travels as **one** variable.

    🔴 Two variables can drift apart, and "right repository, another version's
    commit" is the failure being avoided: it trains happily and is then judged
    against a different base. One string cannot half-update.
    """
    command = build_docker_command(
        train_data_path="/tmp/x/train.json",
        output_dir="/tmp/out",
        base_weights="repo/name@deadbeef",
    )
    passed = [part for part in command if part.startswith("BASE_WEIGHTS=")]
    assert passed == ["BASE_WEIGHTS=repo/name@deadbeef"]


def test_the_published_training_proof_carries_the_season_base_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """🔴 The file that actually ships, not the argument that produces it.

    `training_proof.json` is uploaded into the miner's public repository. It
    used to state `"model": "pi05"` for every season, so a LingBot checkpoint
    shipped with a proof claiming a π0.5 base -- and there is no `config` key,
    because the training config name lives inside the image and the container
    never reports it to the host.
    """
    import openroboto.training.run as run_module

    episodes = tmp_path / "train.json"
    episodes.write_text(
        json.dumps(
            [
                {
                    "observation": {"image": "x", "state": [0.0]},
                    "actions": [[0.0]],
                    "prompt": "do the thing",
                }
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(run_module, "run_training", lambda **kwargs: ({}, {}))
    out = tmp_path / "out"
    run_module.train_once(
        train_json_path=str(episodes),
        output_dir=str(out),
        checkpoint_path="",
        params=run_module.TrainParams(),
        hotkey="5X",
        base_model="lingbot-vla-2.0",
    )

    snapshot = json.loads((out / "training_proof.json").read_text())["config_snapshot"]
    assert snapshot["model"] == "lingbot-vla-2.0"
    assert "config" not in snapshot
