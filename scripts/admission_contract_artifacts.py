"""Load the pinned synthetic fixtures beside the CLI's bundled worker bytes."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tarfile
import zipfile
from importlib.resources import files
from pathlib import Path
from typing import Any

CONTRACT_PATH = Path("tests/contracts/admission_contract")


def verify_digest(path: Path, expected: str) -> None:
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != expected:
        raise ValueError(
            f"{path.name}: SHA-256 mismatch: expected {expected}, got {actual}"
        )


def extract_contract(archive: Path, output: Path) -> None:
    """The pinned archive contains only the synthetic test contract directory."""
    with tarfile.open(archive, "r:gz") as bundle:
        members = bundle.getmembers()
        for member in members:
            destination = (output / member.name).resolve()
            if not destination.is_relative_to((output / CONTRACT_PATH).resolve()):
                raise ValueError(
                    f"Contract archive path is outside its root: {member.name}"
                )
            if not member.isfile() and not member.isdir():
                raise ValueError(
                    f"Contract archive contains a non-regular entry: {member.name}"
                )
        for member in members:
            destination = output / member.name
            if member.isdir():
                destination.mkdir(parents=True, exist_ok=True)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                source = bundle.extractfile(member)
                if source is None:
                    raise ValueError(f"Cannot read contract member: {member.name}")
                destination.write_bytes(source.read())


def extract_worker(output: Path) -> dict[str, Any]:
    context = Path(str(files("openroboto").joinpath("runner/task")))
    release = json.loads((context / "release.json").read_text())
    wheels = [name for name in release["files"] if name.endswith(".whl")]
    if len(wheels) != 1:
        raise ValueError("The official release must contain one task runtime wheel")
    wheel = context / wheels[0]
    verify_digest(wheel, release["files"][wheels[0]])
    with zipfile.ZipFile(wheel) as package:
        for member in package.infolist():
            destination = (output / member.filename).resolve()
            if not destination.is_relative_to(output.resolve()):
                raise ValueError(
                    f"Runtime wheel path is outside its root: {member.filename}"
                )
        package.extractall(output)
    return release


def runtime_environment(root: Path) -> dict[str, str]:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(root), str(root / "tests/contracts")]
    )
    environment["TASK_SOURCE_CONFIG"] = str(root / CONTRACT_PATH / "config.json")
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return environment


def probe_identity(root: Path, *, python: str = sys.executable) -> dict[str, Any]:
    code = (
        "import json; "
        "from admission_contract.generate import content_identity; "
        "print(json.dumps(content_identity(),sort_keys=True))"
    )
    completed = subprocess.run(
        [python, "-c", code],
        cwd=root,
        env=runtime_environment(root),
        capture_output=True,
        text=True,
        check=True,
    )
    return dict(json.loads(completed.stdout))


def prepare_contract(
    lock_path: Path, output: Path, *, python: str = sys.executable
) -> dict[str, Any]:
    lock = json.loads(lock_path.read_text())
    archive = (lock_path.parent / lock["archive"]).resolve()
    if archive.parent != lock_path.parent.resolve():
        raise ValueError("The contract archive must live beside its lock")
    verify_digest(archive, lock["archive_sha256"])
    output.mkdir(parents=True, exist_ok=False)
    extract_contract(archive, output)
    release = extract_worker(output)
    baseline = json.loads((output / CONTRACT_PATH / "baseline.json").read_text())
    identity = probe_identity(output, python=python)
    identity["validator_commit"] = lock["validator_commit"]
    if identity != baseline["identity"] or identity != lock["identity"]:
        raise ValueError(
            "Admission version mismatch between the fixture lock, worker baseline "
            f"and CLI runtime bytes: actual={identity}, baseline={baseline['identity']}"
        )
    if identity["admission_version"] != release["admission_version"]:
        raise ValueError(
            "Admission version mismatch between runtime code and release metadata"
        )
    return {"root": output, "baseline": baseline, "identity": identity, "lock": lock}
