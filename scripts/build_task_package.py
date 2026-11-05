#!/usr/bin/env python3
"""Build a task package for one task from the official AXIS release archive.

The validator repository ships the frozen official bundle
(``configs/benchmarks/axis_v2.0.tar.zst``) with a release receipt. This script
turns one task out of that bundle into the package format the task track reads:

    <out>/
      task.json                 assembled from the official task entry
      benchmark.json            copied verbatim from the official archive
      randomization.json        official file + the loader's `instance` ordinal
      payloads/<id>-official-NN.json
                                copied verbatim (official MJCF embedded)
      provenance/               release receipt and the archive's own manifests

Nothing about the task is invented: the MJCF, checker configuration, initial
states and payload hashes all come from the frozen official bundle. The one
addition is ``instance``, the ordinal of each variant, because the package
loader indexes variants by it while the official manifest uses ``variant_id``.

The output is not uploaded anywhere by this script. Run ``openroboto task
check <out>`` before submitting.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
from typing import Any


def _repo_src() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parents[1] / "src"


sys.path.insert(0, str(_repo_src()))

from openroboto.task_package import (  # noqa: E402
    canonical_json_sha256,
    package_digest,
)


def _read_json(path: pathlib.Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: pathlib.Path, value: Any, *, indent: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(value, ensure_ascii=False, indent=indent, sort_keys=True)
    path.write_text(text + "\n", encoding="utf-8")


def extract_zst(archive: pathlib.Path, destination: pathlib.Path) -> None:
    """Extract the release archive.

    Python 3.11's ``tarfile`` cannot read zstd, and the official archive is
    zstd-compressed. ``tar --zstd`` is the decompressor every target machine
    here already has.
    """
    result = subprocess.run(
        ["tar", "--zstd", "-xf", str(archive), "-C", str(destination)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(
            f"tar --zstd could not extract {archive}:\n{result.stderr.strip()}"
        )


def verify_archive(archive: pathlib.Path, receipt: pathlib.Path | None) -> None:
    if receipt is None:
        return
    data = _read_json(receipt)
    expected_bytes = data.get("bytes")
    expected_sha = data.get("sha256")
    actual = archive.read_bytes()
    if isinstance(expected_bytes, int) and len(actual) != expected_bytes:
        raise SystemExit(
            f"archive is {len(actual)} bytes, receipt pins {expected_bytes}"
        )
    if isinstance(expected_sha, str):
        actual_sha = hashlib.sha256(actual).hexdigest()
        if actual_sha != expected_sha:
            raise SystemExit(
                f"archive sha256 {actual_sha} does not match receipt {expected_sha}"
            )


def build_task(
    release_dir: pathlib.Path,
    task_id: int,
    out: pathlib.Path,
    *,
    receipt: pathlib.Path | None,
) -> dict[str, Any]:
    benchmark = _read_json(release_dir / "benchmark.json")
    randomization = _read_json(release_dir / "randomization.json")

    tasks = [
        item for item in benchmark.get("tasks") or [] if item.get("task_id") == task_id
    ]
    if len(tasks) != 1:
        raise SystemExit(f"benchmark.json has {len(tasks)} tasks with id {task_id}")
    task_entry = tasks[0]

    rand_tasks = [
        item
        for item in randomization.get("tasks") or []
        if item.get("task_id") == task_id
    ]
    if len(rand_tasks) != 1:
        raise SystemExit(
            f"randomization.json has {len(rand_tasks)} tasks with id {task_id}"
        )
    rand_task = rand_tasks[0]
    variants = rand_task.get("variants") or []
    if not variants:
        raise SystemExit(f"task {task_id} has no instances in randomization.json")

    first_payload = _read_json(release_dir / str(variants[0]["payload_path"]))
    if canonical_json_sha256(first_payload.get("checker_config")) != task_entry.get(
        "checker_sha256"
    ):
        raise SystemExit("first payload's checker does not match the benchmark hash")
    if canonical_json_sha256(
        first_payload.get("initial_state") or {}
    ) != task_entry.get("initial_state_sha256"):
        raise SystemExit(
            "first payload's initial state does not match the benchmark hash"
        )

    if out.exists():
        raise SystemExit(f"{out} already exists; refusing to overwrite a package")
    out.mkdir(parents=True)

    for variant in variants:
        relative = str(variant["payload_path"])
        source = release_dir / relative
        destination = out / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)

    # The official variant list identifies instances by `variant_id`; the
    # package loader reads `instance`. Add the ordinal, keep every official
    # hash field untouched so the canonical hashes still match.
    package_randomization = json.loads(json.dumps(randomization))
    package_rand_task = next(
        item
        for item in package_randomization["tasks"]
        if item.get("task_id") == task_id
    )
    for index, variant in enumerate(package_rand_task["variants"]):
        variant["instance"] = index
    _write_json(out / "randomization.json", package_randomization)

    shutil.copyfile(release_dir / "benchmark.json", out / "benchmark.json")

    runtime = benchmark.get("runtime") or {}
    _write_json(
        out / "task.json",
        {
            "schema_version": 1,
            "source": "axis",
            "task_id": task_id,
            "title": task_entry["instruction"],
            "instruction": task_entry["instruction"],
            "checker": first_payload["checker_config"],
            "runtime": {
                key: runtime[key]
                for key in ("control_period_s", "observation_joint_order", "camera")
                if key in runtime
            },
        },
        indent=1,
    )

    provenance = out / "provenance"
    for name in ("bindings.json", "validation.json"):
        source = release_dir / name
        if source.is_file():
            provenance.mkdir(exist_ok=True)
            shutil.copyfile(source, provenance / name)
    if receipt is not None and receipt.is_file():
        provenance.mkdir(exist_ok=True)
        shutil.copyfile(receipt, provenance / "release.json")

    (provenance / "README.md").write_text(
        "# Provenance\n\n"
        "Derived from the official AXIS release archive. Payloads, checker\n"
        "configuration, initial states and hashes are copied verbatim; the\n"
        "only added key is `instance` (the variant ordinal) in\n"
        "`randomization.json`, which the package loader requires. See\n"
        "`release.json` for the archive's pinned hashes.\n",
        encoding="utf-8",
    )

    return {
        "task_id": task_id,
        "package": str(out),
        "instances": len(variants),
        "package_digest": package_digest(out),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", required=True, help="path to axis_v2.0.tar.zst")
    parser.add_argument("--task-id", required=True, type=int)
    parser.add_argument("--out", required=True, help="new directory for the package")
    parser.add_argument(
        "--receipt",
        default="",
        help="optional axis_v2.0-release.json; verifies the archive bytes/sha256 "
        "and is copied into the package's provenance/",
    )
    args = parser.parse_args()

    archive = pathlib.Path(args.release).expanduser().resolve()
    receipt = (
        pathlib.Path(args.receipt).expanduser().resolve() if args.receipt else None
    )
    out = pathlib.Path(args.out).expanduser().resolve()
    if not archive.is_file():
        raise SystemExit(f"release archive not found: {archive}")
    verify_archive(archive, receipt)

    with tempfile.TemporaryDirectory(prefix="axis-release-") as temp:
        release_dir = pathlib.Path(temp)
        extract_zst(archive, release_dir)
        summary = build_task(release_dir, args.task_id, out, receipt=receipt)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
