"""Build and run the official bundled CPU image, like the training runner."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from importlib.resources import files
from pathlib import Path
from typing import Any

from openroboto.config import ConfigError, Settings


def runtime_release() -> dict[str, Any]:
    return dict(
        json.loads(files("openroboto").joinpath("runner/task/release.json").read_text())
    )


def resolve_runtime(settings: Settings) -> dict[str, Any]:
    release = runtime_release()
    policy = settings.competition_params.get("task", {})
    profile = policy.get("execution_profile")
    if profile not in release["execution_profiles"]:
        raise ConfigError(
            "No bundled self-test runtime matches "
            "params.task.execution_profile. Update the CLI or use the "
            "matching scene workflow."
        )
    declared = policy.get("runtime", {})
    for key in ("version", "content_sha256", "admission_version"):
        expected = release[key]
        if key in declared and declared[key] != expected:
            raise ConfigError(
                f"Season runtime.{key}={declared[key]!r} differs from "
                f"this CLI's {expected!r}. Install the season's CLI release."
            )
        if (
            key in settings.authoring.get("runtime", {})
            and settings.authoring["runtime"][key] != expected
        ):
            raise ConfigError(
                f"authoring.runtime.{key} differs from the bundled runtime. "
                "Run init in a new directory using the season's CLI release."
            )
    return release


def image_command(
    image: str, arguments: list[str], mounts: list[tuple[Path, bool]], name: str
) -> list[str]:
    command = [
        "docker",
        "run",
        "--rm",
        "--name",
        name,
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--pids-limit=256",
        "--cpus=2",
        "--memory=4g",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--tmpfs",
        "/tmp:rw,nosuid,size=512m",
    ]
    for path, writable in mounts:
        command += [
            "--mount",
            f"type=bind,src={path},dst={path}" + ("" if writable else ",readonly"),
        ]
    return [*command, image, *arguments]


class TaskRuntime:
    def __init__(self, settings: Settings):
        self.release = resolve_runtime(settings)
        self.context = Path(str(files("openroboto").joinpath("runner/task")))
        self.tag = (
            f"openroboto-task-runtime:{self.release['version']}-"
            f"{self.release['content_sha256'][:12]}"
        )
        self.image = ""

    @contextmanager
    def python_bridge(self, mounts: list[tuple[Path, bool]]) -> Iterator[str]:
        """Run the existing strict scene bridge inside one owned container."""
        name = "openroboto-scene-authoring-" + uuid.uuid4().hex
        command = image_command(
            self.image, ["-c", "import time; time.sleep(950)"], mounts, name
        )
        command[2:2] = ["--detach", "--entrypoint", "python"]
        # This control directory is never mounted: author code cannot replace
        # the host executable used for the next identity probe.
        with tempfile.TemporaryDirectory(prefix="openroboto-scene-control-") as folder:
            wrapper = Path(folder) / "runtime-python"
            wrapper.write_text(
                "#!/usr/bin/env python3\nimport os, sys\n"
                f"command = ['docker', 'exec', {name!r}, 'python', *sys.argv[1:]]\n"
                "os.execvp('docker', command)\n"
            )
            wrapper.chmod(0o700)
            running = False
            try:
                started = subprocess.run(
                    command, capture_output=True, text=True, check=False, timeout=30
                )
                if started.returncode:
                    raise ConfigError(
                        "Cannot start scene authoring container: "
                        f"{started.stderr[-2000:]}"
                    )
                running = True
                yield str(wrapper)
            except (OSError, subprocess.SubprocessError) as exc:
                raise ConfigError(
                    f"Cannot run the official scene container: {exc}. "
                    "Run openroboto doctor and retain the failed attempt."
                ) from exc
            finally:
                # Killing a docker-exec client alone does not kill native work.
                try:
                    stopped = subprocess.run(
                        ["docker", "rm", "-f", name],
                        capture_output=True,
                        check=False,
                        timeout=30,
                    )
                except (OSError, subprocess.SubprocessError) as exc:
                    raise ConfigError(
                        "Cannot stop the scene container. Run docker rm -f " + name
                    ) from exc
                if running and stopped.returncode and sys.exception() is None:
                    raise ConfigError(
                        "Cannot remove the scene authoring container; no check "
                        "receipt was issued. Stop it with docker rm -f " + name
                    )

    def prepare(self, *, build: bool = True) -> None:
        try:
            actual = hashlib.sha256()
            for name, expected in sorted(self.release["files"].items()):
                digest = hashlib.sha256((self.context / name).read_bytes()).hexdigest()
                if digest != expected:
                    raise ConfigError(
                        f"Bundled runtime artifact changed: {name}. "
                        "Reinstall this CLI release."
                    )
                actual.update(f"{name}\0{digest}\n".encode())
            if actual.hexdigest() != self.release["content_sha256"]:
                raise ConfigError(
                    "Bundled runtime content digest does not match "
                    "release.json. Reinstall the CLI."
                )
            result = subprocess.run(
                ["docker", "image", "inspect", self.tag],
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
            if result.returncode:
                if not build:
                    raise ConfigError(
                        "Task runtime image is missing. Run openroboto build or "
                        "openroboto check to prepare it."
                    )
                command = [
                    "docker",
                    "build",
                    "--build-arg",
                    f"RUNTIME_CONTENT_SHA256={self.release['content_sha256']}",
                    "-t",
                    self.tag,
                    str(self.context),
                ]
                built = subprocess.run(
                    command, capture_output=True, text=True, check=False, timeout=1800
                )
                if built.returncode:
                    raise ConfigError(
                        f"Task image build failed: {built.stderr[-4000:]}\n"
                        "Run openroboto build after fixing Docker/network access."
                    )
                result = subprocess.run(
                    ["docker", "image", "inspect", self.tag],
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=30,
                )
            metadata = json.loads(result.stdout)[0]
            labels = metadata["Config"]["Labels"] or {}
            if (
                labels.get("org.openroboto.runtime.content-sha256")
                != self.release["content_sha256"]
            ):
                raise ConfigError(
                    f"Image {self.tag} does not match this release. "
                    "Remove that image and run openroboto build."
                )
            self.image = metadata["Id"]
            description = self.run(["describe"])
            for key in (
                "runtime_version",
                "protocol_version",
                "admission_version",
                "execution_profiles",
                "admission_policy_sha256",
            ):
                expected = (
                    self.release["version"]
                    if key == "runtime_version"
                    else self.release[key]
                )
                if description.get(key) != expected:
                    raise ConfigError(
                        f"Image {key} mismatch. "
                        "Rebuild with this CLI's official context."
                    )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ConfigError(
                f"Cannot prepare Docker task runtime: {exc}. "
                "Install/start Docker, then run openroboto doctor."
            ) from exc

    def run(
        self,
        arguments: list[str],
        mounts: list[tuple[Path, bool]] | None = None,
        *,
        timeout: int = 180,
    ) -> dict[str, Any]:
        name = "openroboto-self-test-" + uuid.uuid4().hex
        command = image_command(self.image, arguments, mounts or [], name)
        try:
            result = subprocess.run(
                command, capture_output=True, text=True, check=False, timeout=timeout
            )
        except subprocess.TimeoutExpired:
            subprocess.run(
                ["docker", "rm", "-f", name],
                capture_output=True,
                check=False,
                timeout=30,
            )
            raise ConfigError(
                f"Task runtime exceeded {timeout}s; no passing verdict "
                "was produced. Inspect evidence and solve.py before retrying."
            ) from None
        except OSError as exc:
            raise ConfigError(
                f"Cannot start Docker: {exc}. Run openroboto doctor."
            ) from exc
        try:
            document: dict[str, Any] = json.loads(result.stdout)
        except ValueError as exc:
            raise ConfigError(
                f"Task runtime failed (exit {result.returncode}): "
                f"{result.stderr[-2000:]}. Run openroboto doctor."
            ) from exc
        if result.returncode and document.get("accepted") is not False:
            raise ConfigError(f"Task runtime exited {result.returncode}: {document}")
        return document
