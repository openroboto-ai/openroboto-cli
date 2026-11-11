"""Run the real CLI structural handler against the pinned worker test transport.

Only the external runtime transport is substituted. The CLI parser/handler and
worker admission/checker implementation execute normally. This regression does
not exercise Docker, season profile routing, recording, or physical admission;
every CLI result must explicitly remain unready for submission.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

from admission_contract_artifacts import (
    CONTRACT_PATH,
    AdmissionIdentityMismatch,
    prepare_contract,
    runtime_environment,
)
from check_admission_parity import compare_bundles, compare_value

SCRIPT_ROOT = Path(__file__).resolve().parent
CASE_ROOT: Path | None = None
WORKER_RAW: Path | None = None


class ContractRuntime:
    """Transport the CLI's structural request to the real public worker entry."""

    def __init__(self, settings):
        from openroboto.task_runtime import runtime_release

        if CASE_ROOT is None or WORKER_RAW is None:
            raise ValueError("The fixed regression transport has not been initialized")
        self.root = CASE_ROOT
        self.raw = WORKER_RAW
        self.release = runtime_release()
        self.image = ""

    def prepare(self):
        # Artifact identity was checked before any case subprocess was started.
        if not (self.root / CONTRACT_PATH / "suite.json").is_file():
            raise ValueError("The pinned fixture suite is missing")

    def run(self, arguments, mounts=None, **kwargs):
        if not arguments or arguments[0] != "admit-static":
            raise ValueError(
                "The regression transport only permits structural admission"
            )
        package = Path(arguments[1]).resolve()
        if package.parent != (self.root / CONTRACT_PATH / "cases").resolve():
            raise ValueError(
                "The regression transport only accepts the pinned fixture cases"
            )
        if mounts != [(package, False)]:
            raise ValueError(
                "The regression transport requires a read-only package mount"
            )
        expected = ["--execution-profile", "null", "--source", "axis"]
        if arguments[2:] != expected:
            raise ValueError(
                "The generic verdict suite has no season execution profile"
            )
        completed = subprocess.run(
            [sys.executable, "-m", "task_source.cli", "admit", str(package), "--json"],
            cwd=self.root,
            env=runtime_environment(self.root),
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        self.raw.with_suffix(".worker.stdout.json").write_text(completed.stdout)
        self.raw.with_suffix(".worker.stderr.txt").write_text(completed.stderr)
        envelope = json.loads(completed.stdout)
        if envelope["errors"] or len(envelope["results"]) != 1:
            raise ValueError(
                f"The real worker did not return one fixture verdict: {envelope}"
            )
        verdict = envelope["results"][0]
        if completed.returncode != (0 if verdict["accepted"] else 1):
            raise ValueError("The worker process exit status differs from its verdict")
        return verdict


def invoke_cli(root: str, case: str, config: str, output: str) -> int:
    """Process boundary used by the suite, with the real CLI main entry point."""
    global CASE_ROOT, WORKER_RAW
    from task_source.package import package_digest

    from openroboto.cli import main as cli_main

    CASE_ROOT = Path(root).resolve()
    WORKER_RAW = Path(output).resolve()
    package = CASE_ROOT / CONTRACT_PATH / "cases" / case
    before = package_digest(package)
    with patch("openroboto.commands.task_check.TaskRuntime", ContractRuntime):
        code = cli_main(
            [
                "check",
                str(package),
                "--structural-only",
                "--json",
                "--config",
                config,
            ]
        )
    after = package_digest(package)
    WORKER_RAW.with_suffix(".identity.json").write_text(
        json.dumps(
            {
                "package_digest_before": before,
                "package_digest": after,
                "transport": "fixed worker structural subprocess",
                "physical_admission": "not_run",
            },
            indent=2,
        )
        + "\n"
    )
    if before != after:
        raise ValueError(f"{case}: structural admission changed package contents")
    return code


def capture(prepared: dict[str, Any], output: Path, *, python: str) -> dict[str, Any]:
    root, baseline = prepared["root"], prepared["baseline"]
    sys.path.insert(0, str(root / "tests/contracts"))
    from admission_contract.generate import normalize_verdict

    raw = output / "raw"
    raw.mkdir()
    config = root / "miner.json"
    config.write_text(
        json.dumps(
            {
                "competition": {
                    "id": 288,
                    "track": "task",
                    "seq": 1,
                    "params": {
                        "task": {"source": "axis", "execution_profile": None},
                    },
                },
                "backend": {"url": "http://127.0.0.1:1"},
                "authoring": {"evidence": "evidence"},
            }
        )
    )
    results, errors = [], []
    bootstrap = (
        "import sys; sys.path.insert(0,sys.argv[1]); "
        "from capture_cli_admission import invoke_cli; "
        "raise SystemExit(invoke_cli(*sys.argv[2:]))"
    )
    for expected in baseline["results"]:
        case = expected["case"]
        locator = raw / case
        command = [
            python,
            "-c",
            bootstrap,
            str(SCRIPT_ROOT),
            str(root),
            case,
            str(config),
            str(locator),
        ]
        completed = subprocess.run(
            command,
            cwd=root,
            env=runtime_environment(root),
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        locator.with_suffix(".cli.stdout.json").write_text(completed.stdout)
        locator.with_suffix(".cli.stderr.txt").write_text(completed.stderr)
        try:
            cli = json.loads(completed.stdout)
            if cli["scope"] != "structural-only" or cli["ready_to_submit"] is not False:
                raise ValueError(
                    "The CLI structural stage must not claim full readiness"
                )
            if "receipt" in cli or (root / "evidence/check-receipt.json").exists():
                raise ValueError("The structural stage must not issue a submit receipt")
            metadata = json.loads(locator.with_suffix(".identity.json").read_text())
            verdict = normalize_verdict(cli, root / CONTRACT_PATH / "cases" / case)
            results.append(
                {
                    "case": case,
                    "package_digest": metadata["package_digest"],
                    "exit_code": completed.returncode,
                    "verdict": verdict,
                    "cli_scope": cli["scope"],
                    "ready_to_submit": cli["ready_to_submit"],
                }
            )
        except (OSError, KeyError, ValueError) as exc:
            errors.append(
                {"case": case, "error": str(exc), "exit_code": completed.returncode}
            )
    return {
        "schema_version": 1,
        "suite": baseline["suite"],
        "scope": baseline["scope"],
        "generated_at": datetime.now(UTC).isoformat(),
        "identity": prepared["identity"],
        "transport_scope": (
            "real CLI main/structural handler; fixed worker subprocess; "
            "Docker/profile routing not exercised"
        ),
        "results": results,
        "endpoint_errors": errors,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--lock",
        type=Path,
        default=SCRIPT_ROOT.parent / "tests/fixtures/admission_parity/contract.json",
    )
    parser.add_argument(
        "--output-dir", type=Path, required=True, help="new evidence directory"
    )
    parser.add_argument(
        "--python",
        default=sys.executable,
        help="Python with the candidate CLI installed",
    )
    args = parser.parse_args(argv)
    args.output_dir = args.output_dir.resolve()
    args.output_dir.mkdir(parents=True, exist_ok=False)
    stage = "identity"
    try:
        prepared = prepare_contract(
            args.lock.resolve(), args.output_dir / "runtime", python=args.python
        )
        (args.output_dir / "worker-baseline.json").write_text(
            json.dumps(prepared["baseline"], indent=2, sort_keys=True) + "\n"
        )
        (args.output_dir / "identity.json").write_text(
            json.dumps(prepared["identity"], indent=2, sort_keys=True) + "\n"
        )
        stage = "verdict"
        candidate = capture(prepared, args.output_dir, python=args.python)
        (args.output_dir / "cli-verdicts.json").write_text(
            json.dumps(candidate, indent=2, sort_keys=True) + "\n"
        )
        if candidate["endpoint_errors"]:
            report = {
                "schema_version": 1,
                "status": "endpoint_error",
                "errors": candidate["endpoint_errors"],
            }
        else:
            report = compare_bundles(prepared["baseline"], candidate)
    except AdmissionIdentityMismatch as exc:
        report = {
            "schema_version": 1,
            "status": "version_mismatch",
            "expected_identity": exc.expected,
            "actual_identity": exc.actual,
            "differences": compare_value(exc.expected, exc.actual, exc.component),
        }
        (args.output_dir / "identity.json").write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n"
        )
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        report = {
            "schema_version": 1,
            "status": "preflight_failed" if stage == "identity" else "execution_failed",
            "error": str(exc),
        }
    report.update(
        component="cli", stage=stage, scope="structural-only", ready_to_submit=False
    )
    (args.output_dir / "comparison.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n"
    )
    if report.get("error"):
        print(report["error"], file=sys.stderr)
    for change in report.get("differences", []):
        print(
            f"{change['field']}: worker={change['expected']!r}, "
            f"cli={change['actual']!r}",
            file=sys.stderr,
        )
    for error in report.get("errors", []):
        print(f"{error['case']}: {error['error']}", file=sys.stderr)
    for case in report.get("results", []):
        for change in case["differences"]:
            print(
                f"{case['case']}: {change['field']}: "
                f"worker={change['expected']!r}, cli={change['actual']!r}",
                file=sys.stderr,
            )
    print(
        f"Structural admission parity: {report['status']}; "
        f"physical admission not run; evidence: {args.output_dir}"
    )
    return 0 if report["status"] == "matched" else 1


if __name__ == "__main__":
    raise SystemExit(main())
