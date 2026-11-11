#!/usr/bin/env python3
"""Verify the CLI under a **normally installed** candidate protocol wheel.

The OPE-209 staging evidence used `PYTHONPATH` to splice a temporary protocol
tree (v0.12.0 + the TASK commit). The review rightly refused that as evidence
that a miner can `pip install` and use the CLI. This script reproduces the
real install path in a throwaway copy and a fresh venv:

1. copy the working tree (no `.git`, no caches) to a temporary directory;
2. rewrite the `openroboto-protocol==...` pin there to the candidate wheel's
   version (the working tree's pin is **not** modified);
3. build the CLI wheel from that copy;
4. create a fresh venv, install the candidate protocol wheel and then the CLI
   wheel with normal dependency resolution;
5. run the installed `openroboto` binary -- no `PYTHONPATH`, no source tree on
   `sys.path` -- and check:
   - `Track` admits `task` and `model_format` has `AXIS_LAYOUT`;
   - `openroboto check <package>` reports the expected digest;
   - optionally `openroboto status --probe --config <miner.yaml>`.

The candidate wheel is a **candidate**, not a published release. This proves
the install shape; it does not make the version pip-installable from PyPI.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
PIN_RE = re.compile(r"(openroboto-protocol\s*==\s*)([0-9A-Za-z.+-]+)")
IGNORE = shutil.ignore_patterns(
    ".git",
    ".venv",
    "dist",
    "build",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "*.egg-info",
)


def run(
    cmd: list[str], *, cwd: pathlib.Path | None = None, env: dict | None = None
) -> str:
    result = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(
            f"command failed ({result.returncode}): {' '.join(cmd)}\n"
            f"stdout:\n{result.stdout[-3000:]}\n"
            f"stderr:\n{result.stderr[-3000:]}"
        )
    return result.stdout.strip()


def shared_digest(
    python: pathlib.Path,
    package: str,
    module: str,
    function: str,
) -> str:
    """Run the shared implementation in the fresh venv; **fail closed**.

    The acceptance question is "does the candidate wheel expose the shared
    digest and does it agree with the CLI". A missing module, a missing
    function, a non-zero exit, malformed output or a non-64-hex value is a
    failure, never an `available: false` field that lets the script exit 0.
    There is deliberately no fail-open diagnostic mode: a probe that was told
    to look for a missing function must be red.
    """
    result = subprocess.run(
        [
            str(python),
            "-c",
            "import json, importlib; "
            f"m = importlib.import_module({module!r}); "
            f"fn = getattr(m, {function!r}); "
            f"print(json.dumps({{'digest': fn({package!r})}}))",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = (
            result.stderr.strip().splitlines()[-1]
            if result.stderr.strip()
            else "no stderr"
        )
        raise SystemExit(f"shared digest probe failed: {module}.{function}: {detail}")
    try:
        payload = json.loads(result.stdout)
    except ValueError as exc:
        raise SystemExit(
            f"shared digest probe returned non-JSON: {result.stdout!r}"
        ) from exc
    digest = payload.get("digest") if isinstance(payload, dict) else None
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(char not in "0123456789abcdef" for char in digest)
    ):
        raise SystemExit(
            f"shared digest probe returned {digest!r}, not a 64-character "
            f"lowercase sha256"
        )
    return digest


def wheel_version(wheel: pathlib.Path) -> str:
    """Read the version out of a wheel's METADATA without installing it."""
    with zipfile.ZipFile(wheel) as archive:
        names = [n for n in archive.namelist() if n.endswith(".dist-info/METADATA")]
        if not names:
            raise SystemExit(f"{wheel} holds no dist-info/METADATA")
        text = archive.read(names[0]).decode("utf-8", "replace")
    name = version = None
    for line in text.splitlines():
        if line.startswith("Name: "):
            name = line[6:].strip()
        elif line.startswith("Version: "):
            version = line[9:].strip()
    if name is None or version is None:
        raise SystemExit(f"{wheel} METADATA has no Name/Version")
    return version


def copy_tree(destination: pathlib.Path) -> pathlib.Path:
    shutil.copytree(ROOT, destination, ignore=IGNORE)
    return destination


def rewrite_pin(pyproject: pathlib.Path, version: str) -> tuple[str, str]:
    text = pyproject.read_text(encoding="utf-8")
    match = PIN_RE.search(text)
    if match is None:
        raise SystemExit(f"{pyproject} has no `openroboto-protocol==` pin")
    old = match.group(2)
    pyproject.write_text(
        text[: match.start(2)] + version + text[match.end(2) :],
        encoding="utf-8",
    )
    return old, version


def venv_python(venv: pathlib.Path) -> pathlib.Path:
    return venv / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--protocol-wheel", required=True)
    parser.add_argument("--package", default="", help="task package for `check`")
    parser.add_argument(
        "--scene-package", default="", help="dual-root input for scene build/check"
    )
    parser.add_argument(
        "--config", default="", help="miner.yaml for `status --probe`"
    )
    parser.add_argument(
        "--status-config",
        default="",
        help="miner.yaml with a signable hotkey wallet for `status`",
    )
    parser.add_argument(
        "--run-id", default="", help="operator run id recorded in the output"
    )
    parser.add_argument(
        "--expect-digest",
        default="",
        help="fail when `check` reports a different package digest",
    )
    parser.add_argument(
        "--shared-module",
        default="openroboto_protocol.package_digest",
        help="module in the candidate protocol wheel that owns the shared digest",
    )
    parser.add_argument(
        "--shared-function",
        default="package_digest",
        help="digest function in --shared-module",
    )
    parser.add_argument("--keep", action="store_true", help="keep the temp tree/venv")
    args = parser.parse_args()

    protocol_wheel = pathlib.Path(args.protocol_wheel).expanduser().resolve()
    if not protocol_wheel.is_file():
        raise SystemExit(f"candidate protocol wheel not found: {protocol_wheel}")
    version = wheel_version(protocol_wheel)

    temp = pathlib.Path(tempfile.mkdtemp(prefix="ope209-normal-install-"))
    try:
        work = copy_tree(temp / "cli")
        old_pin, new_pin = rewrite_pin(work / "pyproject.toml", version)
        dist = work / "dist"
        run(["uv", "build", "--wheel", "--out-dir", str(dist)], cwd=work)
        cli_wheels = sorted(dist.glob("openroboto-*.whl"))
        if not cli_wheels:
            raise SystemExit(f"uv build produced no CLI wheel in {dist}")

        venv = temp / "venv"
        run(["uv", "venv", str(venv)])
        python = venv_python(venv)
        # Install the candidate first: the CLI's pin was rewritten to its
        # version, so normal resolution sees the requirement already satisfied.
        run(["uv", "pip", "install", "--python", str(python), str(protocol_wheel)])
        run(["uv", "pip", "install", "--python", str(python), str(cli_wheels[0])])

        capabilities = run(
            [
                str(python),
                "-c",
                "import json; from openroboto_protocol.commitment import Track; "
                "from openroboto_protocol import model_format; "
                "print(json.dumps({'tracks': [str(t) for t in Track], "
                "'has_task': getattr(Track,'TASK',None) is not None, "
                "'has_axis_layout': hasattr(model_format,'AXIS_LAYOUT')}))",
            ]
        )
        caps = json.loads(capabilities)
        if not caps["has_task"]:
            raise SystemExit(
                f"candidate wheel {protocol_wheel} has no Track.TASK: {caps}"
            )
        if not caps["has_axis_layout"]:
            raise SystemExit(
                f"candidate wheel {protocol_wheel} has no AXIS_LAYOUT: {caps}"
            )

        cli = venv / (
            "Scripts/openroboto.exe" if sys.platform == "win32" else "bin/openroboto"
        )
        version_line = run([str(cli), "--version"])
        summary = {
            "protocol_wheel": str(protocol_wheel),
            "protocol_version": version,
            "pin_before": old_pin,
            "pin_in_temp_copy": new_pin,
            "capabilities": caps,
            "cli_version": version_line,
            "protocol_wheel_sha256": hashlib.sha256(
                protocol_wheel.read_bytes()
            ).hexdigest(),
            "cli_wheel_sha256": hashlib.sha256(cli_wheels[0].read_bytes()).hexdigest(),
        }

        if args.scene_package:
            source = pathlib.Path(args.scene_package).resolve()
            built = temp / "scene-submission"
            summary["scene_build"] = json.loads(
                run(
                    [
                        str(cli),
                        "scene",
                        "build",
                        "--execution",
                        str(source / "execution"),
                        "--private",
                        str(source / "private"),
                        "--output",
                        str(built),
                    ]
                )
            )
            checked = json.loads(run([str(cli), "scene", "check", str(built)]))
            summary["scene_check"] = checked
            if not checked["ok"] or checked["scope"] != "structure_only":
                raise SystemExit(
                    "Scene check did not establish structure-only validity"
                )
            for name, field in (
                ("execution", "execution_package_digest"),
                ("private", "private_package_digest"),
            ):
                actual = shared_digest(
                    python, str(built / name), args.shared_module, args.shared_function
                )
                if actual != checked["submission"][field]:
                    raise SystemExit(f"Scene {name} digest disagrees with the protocol")

        if args.package:
            check_raw = run([str(cli), "check", args.package, "--static-only", "--config", args.config, "--json"])
            check = json.loads(check_raw)
            summary["task_check"] = {
                "ok": check["ok"],
                "source": check["source"],
                "task_id": check["task_id"],
                "files": check["file_count"],
                "package_digest": check["package_digest"],
                "digest_source": check.get("digest_source", ""),
            }
            if args.expect_digest and check["package_digest"] != args.expect_digest:
                raise SystemExit(
                    f"package digest {check['package_digest']} != expected "
                    f"{args.expect_digest}"
                )
            if check.get("ok") is not True:
                raise SystemExit(f"check reported ok={check.get('ok')!r}")
            if check.get("digest_source") != "openroboto-protocol":
                raise SystemExit(
                    f"CLI digest_source is {check.get('digest_source')!r}, not "
                    f"'openroboto-protocol'; the shared implementation did not run"
                )
            shared_value = shared_digest(
                python,
                args.package,
                args.shared_module,
                args.shared_function,
            )
            summary["shared_digest"] = {
                "module": args.shared_module,
                "function": args.shared_function,
                "digest": shared_value,
                "matches_cli": shared_value == check["package_digest"],
            }
            if shared_value != check["package_digest"]:
                raise SystemExit(
                    f"CLI digest {check['package_digest']} != shared digest "
                    f"{shared_value} from {args.shared_module}.{args.shared_function}"
                )

        if args.config:
            probe_raw = run(
                [
                    str(cli),
                    "status",
                    "--probe",
                    "--config",
                    args.config,
                    "--json",
                ]
            )
            probe = json.loads(probe_raw)
            summary["probe"] = {
                "backend": probe["backend"],
                "ready": probe["readiness"]["ready"],
                "alembic_version": probe["readiness"]["alembic_version"],
                "expected_head": probe["readiness"]["expected_head"],
                "netuid": probe["liveness"]["netuid"],
            }

        if args.status_config:
            command = [
                str(cli),
                "status",
                "--config",
                args.status_config,
                "--json",
            ]
            if args.run_id:
                command.extend(["--run-id", args.run_id])
            status_raw = run(command)
            status = json.loads(status_raw)
            summary["status"] = {
                "backend": status["backend"],
                "hotkey": status["hotkey"],
                "request_id": status["request_id"],
                "run_id": status.get("run_id", ""),
                "tasks": len(status["tasks"]),
            }

        print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    finally:
        if args.keep:
            print(f"kept: {temp}", file=sys.stderr)
        else:
            shutil.rmtree(temp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
