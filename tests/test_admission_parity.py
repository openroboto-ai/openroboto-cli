"""Compare every verdict field and reject mismatched versions first."""

import copy
import importlib.util
import json
from pathlib import Path

import pytest


def comparator():
    path = Path(__file__).resolve().parents[1] / "scripts/check_admission_parity.py"
    spec = importlib.util.spec_from_file_location("check_admission_parity", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def bundle():
    return {
        "schema_version": 1,
        "suite": "admission-contract/1",
        "scope": "worker structural/checker verdict only; physical replay not run",
        "identity": {
            "validator_commit": "a" * 40,
            "admission_version": "task-source-miner/1",
            "admission_source_sha256": "b" * 64,
            "fixture_suite_sha256": "c" * 64,
        },
        "results": [
            {
                "case": "positive",
                "package_digest": "d" * 64,
                "exit_code": 0,
                "verdict": {
                    "accepted": True,
                    "code": None,
                    "checks": [
                        {
                            "name": "checker_registered",
                            "passed": True,
                            "kind": "structural",
                            "detail": {"type": "real-checker"},
                        },
                        {
                            "name": "solvable",
                            "passed": True,
                            "kind": "structural",
                            "detail": {"errors": []},
                        },
                    ],
                },
            }
        ],
    }


def test_equal_results_ignore_only_provenance_time():
    expected = bundle()
    actual = copy.deepcopy(expected)
    expected["generated_at"] = "old"
    actual["generated_at"] = "new"
    assert comparator().compare_bundles(expected, actual)["status"] == "matched"


@pytest.mark.parametrize(
    "field",
    [
        "validator_commit",
        "admission_version",
        "admission_source_sha256",
        "fixture_suite_sha256",
    ],
)
def test_identity_mismatch_stops_before_cases(field):
    expected = bundle()
    actual = copy.deepcopy(expected)
    actual["identity"][field] = "wrong"
    actual["results"] = "would fail if inspected"
    report = comparator().compare_bundles(expected, actual)
    assert report["status"] == "version_mismatch"
    assert report["differences"][0]["field"] == "identity." + field
    assert "results" not in report


@pytest.mark.parametrize(
    "field,value",
    [("passed", False), ("kind", "runtime"), ("detail", {"errors": ["one"]})],
)
def test_check_difference_names_case_check_and_value(field, value):
    expected = bundle()
    actual = copy.deepcopy(expected)
    actual["results"][0]["verdict"]["checks"][1][field] = value
    report = comparator().compare_bundles(expected, actual)
    assert report["status"] == "verdict_mismatch"
    case = report["results"][0]
    assert case["case"] == "positive"
    assert case["differences"][0]["field"].startswith("checks[solvable]." + field)
    assert not case["matched"]


@pytest.mark.parametrize(
    "field,value", [("accepted", False), ("code", "reject_unsolvable")]
)
def test_top_level_outcomes_are_compared(field, value):
    expected = bundle()
    actual = copy.deepcopy(expected)
    actual["results"][0]["verdict"][field] = value
    report = comparator().compare_bundles(expected, actual)
    assert report["results"][0]["differences"][0]["field"] == field


def test_missing_check_and_extra_case_are_differences():
    expected = bundle()
    actual = copy.deepcopy(expected)
    actual["results"][0]["verdict"]["checks"].pop()
    extra = copy.deepcopy(actual["results"][0])
    extra["case"] = "extra"
    actual["results"].append(extra)
    report = comparator().compare_bundles(expected, actual)
    assert report["status"] == "verdict_mismatch"
    assert [row["case"] for row in report["results"]] == ["extra", "positive"]
    assert report["results"][1]["differences"][0]["field"] == "checks[solvable]"


def test_duplicate_check_names_and_nonboolean_passed_are_invalid():
    expected = bundle()
    actual = copy.deepcopy(expected)
    actual["results"][0]["verdict"]["checks"].append(
        copy.deepcopy(actual["results"][0]["verdict"]["checks"][0])
    )
    assert comparator().compare_bundles(expected, actual)["status"] == "invalid_input"
    actual = copy.deepcopy(expected)
    actual["results"][0]["verdict"]["checks"][0]["passed"] = 1
    assert comparator().compare_bundles(expected, actual)["status"] == "invalid_input"


def test_comparator_does_not_normalize_semantic_paths():
    expected = bundle()
    actual = copy.deepcopy(expected)
    expected["results"][0]["verdict"]["checks"][1]["detail"] = {
        "file": "reference.json"
    }
    actual["results"][0]["verdict"]["checks"][1]["detail"] = {"file": "different.json"}
    assert (
        comparator().compare_bundles(expected, actual)["status"] == "verdict_mismatch"
    )


def test_command_always_retains_mismatch_json(tmp_path, capsys):
    module = comparator()
    expected = bundle()
    actual = copy.deepcopy(expected)
    actual["results"][0]["verdict"]["checks"][1]["passed"] = False
    baseline = tmp_path / "worker.json"
    baseline.write_text(json.dumps(expected))
    candidate = tmp_path / "cli.json"
    candidate.write_text(json.dumps(actual))
    output = tmp_path / "artifacts/diff.json"
    assert (
        module.main(
            [
                "--baseline",
                str(baseline),
                "--candidate",
                str(candidate),
                "--output",
                str(output),
            ]
        )
        == 1
    )
    assert json.loads(output.read_text())["status"] == "verdict_mismatch"
    assert "positive: checks[solvable].passed" in capsys.readouterr().err


def test_invalid_json_is_retained_as_error(tmp_path):
    baseline = tmp_path / "bad.json"
    baseline.write_text("{broken")
    output = tmp_path / "result.json"
    assert (
        comparator().main(
            [
                "--baseline",
                str(baseline),
                "--candidate",
                str(baseline),
                "--output",
                str(output),
            ]
        )
        == 1
    )
    assert json.loads(output.read_text())["status"] == "invalid_input"


def artifacts():
    path = (
        Path(__file__).resolve().parents[1] / "scripts/admission_contract_artifacts.py"
    )
    spec = importlib.util.spec_from_file_location("admission_contract_artifacts", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def archive_contract(tmp_path, members):
    import io
    import tarfile

    archive = tmp_path / "contract.tar.gz"
    with tarfile.open(archive, "w:gz") as output:
        for name, content, kind in members:
            item = tarfile.TarInfo(name)
            item.type = kind
            item.size = len(content)
            output.addfile(item, io.BytesIO(content))
    return archive


def test_fixture_archive_extracts_only_pinned_contract_files(tmp_path):
    import tarfile

    archive = archive_contract(
        tmp_path,
        [("tests/contracts/admission_contract/suite.json", b"{}", tarfile.REGTYPE)],
    )
    output = tmp_path / "expanded"
    output.mkdir()
    artifacts().extract_contract(archive, output)
    assert (
        output / "tests/contracts/admission_contract/suite.json"
    ).read_text() == "{}"


@pytest.mark.parametrize(
    "name,kind",
    [("../escape", "regular"), ("tests/contracts/admission_contract/link", "symlink")],
)
def test_fixture_archive_cannot_escape_or_link(tmp_path, name, kind):
    import tarfile

    archive = archive_contract(
        tmp_path,
        [(name, b"", tarfile.REGTYPE if kind == "regular" else tarfile.SYMTYPE)],
    )
    with pytest.raises(ValueError, match=r"outside|non-regular"):
        artifacts().extract_contract(archive, tmp_path / "expanded")
    assert not (tmp_path / "escape").exists()


def test_fixture_digest_mismatch_stops_before_unpack(tmp_path):
    module = artifacts()
    archive = tmp_path / "contract.tar.gz"
    archive.write_bytes(b"not the pinned artifact")
    lock = tmp_path / "contract.json"
    lock.write_text(json.dumps({"archive": archive.name, "archive_sha256": "0" * 64}))
    output = tmp_path / "expanded"
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        module.prepare_contract(lock, output)
    assert not output.exists()


def test_contract_source_identity_must_match_all_three_parties(tmp_path, monkeypatch):
    import hashlib
    import tarfile

    module = artifacts()
    baseline = bundle()
    archive = archive_contract(
        tmp_path,
        [
            (
                "tests/contracts/admission_contract/baseline.json",
                json.dumps(baseline).encode(),
                tarfile.REGTYPE,
            )
        ],
    )
    lock = tmp_path / "contract.json"
    lock.write_text(
        json.dumps(
            {
                "archive": archive.name,
                "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                "validator_commit": "a" * 40,
                "identity": baseline["identity"],
            }
        )
    )
    monkeypatch.setattr(
        module,
        "extract_worker",
        lambda root: {"admission_version": "task-source-miner/1"},
    )
    content = {k: v for k, v in baseline["identity"].items() if k != "validator_commit"}
    monkeypatch.setattr(module, "probe_identity", lambda *a, **kw: dict(content))
    assert (
        module.prepare_contract(lock, tmp_path / "success")["identity"]
        == baseline["identity"]
    )
    content["admission_source_sha256"] = "wrong"
    with pytest.raises(ValueError, match="Admission version mismatch"):
        module.prepare_contract(lock, tmp_path / "mismatch")


@pytest.mark.parametrize("value", [[], "text", None])
def test_nonobject_evidence_is_an_invalid_input(value):
    assert comparator().compare_bundles(bundle(), value)["status"] == "invalid_input"


def test_empty_fixture_evidence_does_not_claim_parity():
    empty = bundle()
    empty["results"] = []
    assert comparator().compare_bundles(empty, empty)["status"] == "invalid_input"


def test_shared_fixture_archive_matches_the_bundled_worker_without_checkout(tmp_path):
    module = artifacts()
    lock = Path(__file__).parent / "fixtures/admission_parity/contract.json"
    prepared = module.prepare_contract(lock, tmp_path / "runtime")
    assert len(prepared["baseline"]["results"]) == 9
    assert not (prepared["root"] / ".git").exists()
    assert prepared["identity"] == prepared["baseline"]["identity"]
    assert (
        prepared["identity"]["validator_commit"]
        == "fa1e5b2f9552d0340f58bd654a79b8c44b961d94"
    )
