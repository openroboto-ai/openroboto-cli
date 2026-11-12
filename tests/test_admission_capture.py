"""Exercise the real worker transport and identity preflight boundaries."""

import importlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
LOCK = REPO / "tests/fixtures/admission_parity/contract.json"


@pytest.fixture
def runner(monkeypatch):
    monkeypatch.syspath_prepend(str(REPO / "scripts"))
    return importlib.import_module("capture_cli_admission")


@pytest.fixture(scope="module")
def prepared(tmp_path_factory):
    sys.path.insert(0, str(REPO / "scripts"))
    from admission_contract_artifacts import prepare_contract

    return prepare_contract(LOCK, tmp_path_factory.mktemp("contract") / "runtime")


@pytest.mark.parametrize("case", ["positive", "reference-missing"])
def test_transport_runs_the_real_worker_and_preserves_pending_runtime_checks(
    runner, prepared, tmp_path, monkeypatch, case
):
    monkeypatch.setattr(runner, "CASE_ROOT", prepared["root"])
    monkeypatch.setattr(runner, "WORKER_RAW", tmp_path / case)
    runtime = runner.ContractRuntime(None)
    runtime.prepare()
    package = prepared["root"] / runner.CONTRACT_PATH / "cases" / case
    result = runtime.run(
        [
            "admit-static",
            str(package),
            "--execution-profile",
            "null",
            "--source",
            "axis",
        ],
        [(package, False)],
    )
    assert result["accepted"] is True
    assert (tmp_path / case).with_suffix(".worker.stdout.json").is_file()
    if case == "reference-missing":
        checks = {check["name"]: check for check in result["checks"]}
        for name in ("solvable", "not_freebie"):
            assert checks[name]["passed"] is False
            assert checks[name]["kind"] == "runtime"


@pytest.mark.parametrize("invalid", ["physical", "writable", "foreign", "profile"])
def test_transport_cannot_expand_beyond_the_fixed_readonly_structural_cases(
    runner, prepared, tmp_path, monkeypatch, invalid
):
    monkeypatch.setattr(runner, "CASE_ROOT", prepared["root"])
    monkeypatch.setattr(runner, "WORKER_RAW", tmp_path / "rejected")
    runtime = runner.ContractRuntime(None)
    package = prepared["root"] / runner.CONTRACT_PATH / "cases/positive"
    arguments = [
        "admit-static",
        str(package),
        "--execution-profile",
        "null",
        "--source",
        "axis",
    ]
    mounts = [(package, False)]
    if invalid == "physical":
        arguments[0] = "admit"
    elif invalid == "writable":
        mounts = [(package, True)]
    elif invalid == "foreign":
        arguments[1] = str(tmp_path / "untrusted-package")
    else:
        arguments[3] = '"different-profile"'
    with pytest.raises(ValueError):
        runtime.run(arguments, mounts)
    assert not list(tmp_path.glob("*.worker.*"))


def test_real_capture_process_stops_on_version_mismatch_without_case_results(tmp_path):
    import shutil

    lock = json.loads(LOCK.read_text())
    lock["identity"]["admission_source_sha256"] = "0" * 64
    shutil.copyfile(LOCK.parent / lock["archive"], tmp_path / lock["archive"])
    shutil.copyfile(
        LOCK.parent / lock["protocol_wheel"]["filename"],
        tmp_path / lock["protocol_wheel"]["filename"],
    )
    altered_lock = tmp_path / "contract.json"
    altered_lock.write_text(json.dumps(lock))
    output = tmp_path / "evidence"
    process = subprocess.run(
        [
            sys.executable,
            str(REPO / "scripts/capture_cli_admission.py"),
            "--lock",
            str(altered_lock),
            "--output-dir",
            str(output),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert process.returncode == 1
    report = json.loads((output / "comparison.json").read_text())
    assert report["status"] == "version_mismatch"
    assert report["differences"][0]["field"] == "lock.identity.admission_source_sha256"
    assert "results" not in report
    assert not (output / "cli-verdicts.json").exists()
    assert not (output / "raw").exists()
    assert (output / "identity.json").is_file()
