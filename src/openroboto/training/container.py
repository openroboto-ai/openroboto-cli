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


def runner_image(competition_image: str = "") -> str:
    """The training image name.

    `OPENPI_RUNNER_IMAGE` still wins (a miner building their own image needs it
    to win, and it did before competitions existed). Below it comes the image
    this competition names in `params.training.image`, and below that the
    built-in default -- which is the π0.5 image, the one every config without a
    competition section has always used.
    """
    return os.getenv("OPENPI_RUNNER_IMAGE") or competition_image or DEFAULT_IMAGE


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

    # ⚠️ **The source of every `-v` must be an absolute path.** Docker does not
    # treat a source that does not start with `/` as a path:
    #   - with a slash → it refuses to start the container outright
    #     (`"tmp/…" includes invalid characters for a local volume name`)
    #   - without a slash → it **silently** treats it as a named volume, the
    #     container sees an empty directory, and not a single byte of the
    #     host directory of that name is ever read or written
    #
    # Both were reproduced. The default output root is
    # `Path("./tmp/robot_train_vla_miner")`, and `Path` normalises the `./` away,
    # so `str()` yields `tmp/…` — which means `openroboto train` with the default
    # config **cannot start the container at all**.
    # The base-model cache is the nastier one: `cache` has no slash, raises no
    # error, it just never sees the host cache — several GB re-downloaded every
    # round, while the "cache hit" log line prints as usual.
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
        logger.debug("nvidia-smi unavailable: %s", exc)
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
        logger.info("🎯 Free GPUs detected: %s", ",".join(free))
    else:
        logger.warning("⚠️  No free GPU found, using every card")
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
            logger.info("Removing leftover container %s", container_id)
            subprocess.run(
                ["docker", "rm", "-f", container_id],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("Container cleanup failed (safe to ignore): %s", exc)


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
                "The %s section of the container output is not valid JSON, "
                "falling back to the output directory",
                RESULT_MARKER,
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
    image: str = "",
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run the training container once and return (metrics, proof).

    Raises:
        TrainingError: the data is empty, or the container exited non-zero.
    """
    if not train_samples:
        raise TrainingError(
            "Training set is empty -- check this season's "
            "`competition.params.training.dataset.train` in miner.yaml"
        )

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
            # Empty = whatever `runner_image()` resolves to, which is what every
            # call did before competitions existed. The container **interface**
            # is untouched (red line #2): this only picks which image the same
            # `docker run` line names.
            image=image or None,
        )
        logger.info("🐳 Starting openpi-runner: %s", " ".join(command))

        # These two are not "defensive programming", they are the two environment
        # failures this command is most likely to hit. Without turning them into a
        # TrainingError they propagate raw to the top level: the miner sees
        # `FileNotFoundError: [Errno 2] ... 'docker'` plus twenty lines of
        # traceback, while AGENTS.md §4 requires errors a miner can act on.
        # `build` and `doctor` have done this all along; only this path -- the one
        # that actually drives docker for hours -- was missed.
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
                "docker not found -- training has to run inside a container: "
                "openpi needs numpy<2.0 and bittensor needs numpy>=2.0, and one "
                "interpreter cannot hold both.\n"
                "  \u2192 install Docker: https://get.docker.com\n"
                "  \u2192 then run `openroboto doctor` first, it checks GPU, "
                "drivers and the image in one go"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            hours = TRAIN_TIMEOUT_SEC // 3600
            raise TrainingError(
                f"The training container ran for more than {hours} hours without "
                f"finishing and was aborted.\n"
                f"  This usually is not a bug in your strategy script -- it is "
                f"stuck downloading the base model or the dataset.\n"
                f"  \u2192 run `docker logs {CONTAINER_NAME}` to see which step it "
                f"stopped at\n"
                f"  \u2192 make sure you still have disk space (the base "
                f"checkpoint is several GB)"
            ) from exc

        metrics, proof = parse_result(completed.stdout, output_dir)

    if completed.returncode != 0:
        raise TrainingError(
            f"The training container exited with code {completed.returncode}\n"
            f"  stderr: {completed.stderr[:500]}\n"
            f"  \u2192 run `openroboto doctor` first to confirm GPU, Docker and "
            f"the image are all in place"
        )
    return metrics, proof


class TrainingError(Exception):
    """Training did not run to completion.

    Usually an environment problem (missing image, not enough VRAM), so the error
    message should point at doctor.
    """
