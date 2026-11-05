"""Task packages: the digest rule and the static format checks.

The digest is a cross-repository contract -- the miner, the admission worker
and the backend must agree byte for byte -- so the golden value here was
produced by the validator's own ``task_source/package.py`` against the same
fixture. If this test goes red, one of the two copies drifted and a miner's
package digest would stop matching the one the subnet stores.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from openroboto import task_package as tp

#: Produced by validator ``task_source.package.package_digest`` on exactly the
#: four files written by ``_digest_fixture`` below.
GOLDEN_DIGEST = "c9ed7c7266f5dd8dae6126c9df2012791ec3065a3ea302222277eb9c8e2b6ece"


def _digest_fixture(root: Path) -> Path:
    package = root / "pkg"
    (package / "dir").mkdir(parents=True)
    (package / "a").mkdir()
    (package / "root.txt").write_bytes(b"hello\n")
    (package / "dir" / "b.txt").write_bytes(b"world\n")
    (package / "a" / "c.txt").write_bytes(b"nested\n")
    (package / "a-b.txt").write_bytes(b"\x00binary")
    return package


def test_digest_matches_the_validator_golden(tmp_path: Path) -> None:
    package = _digest_fixture(tmp_path)
    assert tp.package_digest(package) == GOLDEN_DIGEST


def test_files_are_sorted_as_paths_not_as_strings(tmp_path: Path) -> None:
    """`a/c.txt` comes before `a-b.txt` under ``Path`` ordering.

    A string sort puts `a-b.txt` first ('-' < '/'), which produces a different
    digest on exactly the packages that mix nested and dashed names. The
    validator sorts paths; this copy has to.
    """
    files = tp.package_files(_digest_fixture(tmp_path))
    assert [relative for relative, _ in files] == [
        "a/c.txt",
        "a-b.txt",
        "dir/b.txt",
        "root.txt",
    ]


def test_every_file_counts_including_dotfiles(tmp_path: Path) -> None:
    package = _digest_fixture(tmp_path)
    before = tp.package_digest(package)
    (package / ".DS_Store").write_bytes(b"metadata")
    assert tp.package_digest(package) != before, (
        "the validator walks every regular file with no whitelist; a stray file "
        "changes the digest and this copy must see it too"
    )


def test_digest_is_independent_of_the_directory_name(tmp_path: Path) -> None:
    first = _digest_fixture(tmp_path / "one")
    second = _digest_fixture(tmp_path / "two")
    assert tp.package_digest(first) == tp.package_digest(second)


# ── loading ──────────────────────────────────────────────────────────────────


def test_missing_task_json_is_not_a_package(tmp_path: Path) -> None:
    with pytest.raises(tp.TaskPackageError, match=r"missing task\.json"):
        tp.load_package(tmp_path)


def test_unknown_schema_version_is_refused(tmp_path: Path) -> None:
    (tmp_path / "task.json").write_text(
        json.dumps(
            {
                "schema_version": 99,
                "source": "axis",
                "task_id": 1,
                "instruction": "x",
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(tp.TaskPackageError, match="schema_version"):
        tp.load_package(tmp_path)


def test_unknown_source_is_refused(tmp_path: Path) -> None:
    (tmp_path / "task.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source": "gantry",
                "task_id": 1,
                "instruction": "x",
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(tp.TaskPackageError, match="source must be one of"):
        tp.load_package(tmp_path)


# ── AXIS ─────────────────────────────────────────────────────────────────────


CHECKER: dict[str, Any] = {"checker": {"type": "GripperOpenChecker", "threshold": 0.04}}


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")


def axis_package(
    root: Path, *, instruction: str = "Pick up the cube.", drift: bool = False
) -> Path:
    """A minimal well-formed AXIS package (one instance is in the 1..20 range)."""
    root.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "id": 9001,
        "checker_config": CHECKER,
        "initial_state": {"objects": {}, "robots": {}},
        "mjcf_xml": "<mujoco/>",
        "instance": 0,
    }
    state_hash = tp.canonical_json_sha256(payload["initial_state"])
    checker_hash = tp.canonical_json_sha256(CHECKER)
    declared = "0" * 64 if drift else tp.canonical_json_sha256(payload)
    _write_json(
        root / "task.json",
        {
            "schema_version": 1,
            "source": "axis",
            "task_id": 9001,
            "title": "Pick up the cube",
            "instruction": instruction,
            "checker": CHECKER,
            "reference_solution": "reference.json",
        },
    )
    _write_json(
        root / "benchmark.json",
        {
            "tasks": [
                {
                    "task_id": 9001,
                    "instruction": instruction,
                    "checker_sha256": checker_hash,
                    "initial_state_sha256": state_hash,
                }
            ]
        },
    )
    _write_json(
        root / "randomization.json",
        {
            "tasks": [
                {
                    "task_id": 9001,
                    "variants": [
                        {
                            "instance": 0,
                            "payload_path": "payloads/9001-official-00.json",
                            "payload_canonical_sha256": declared,
                            "checker_sha256": checker_hash,
                            "initial_state_sha256": state_hash,
                            "seed": 0,
                        }
                    ],
                }
            ]
        },
    )
    _write_json(root / "payloads" / "9001-official-00.json", payload)
    _write_json(
        root / "reference.json",
        {
            "schema_version": 1,
            "task_id": 9001,
            "instances": {},
        },
    )
    return root


def test_a_well_formed_axis_package_has_no_issues(tmp_path: Path) -> None:
    package = tp.load_package(axis_package(tmp_path / "pkg"))
    assert tp.validate_package(package) == []


def test_axis_hash_drift_is_an_error(tmp_path: Path) -> None:
    package = tp.load_package(axis_package(tmp_path / "pkg", drift=True))
    issues = tp.validate_package(package)
    assert any(issue.code == "axis.payload_mismatch" for issue in issues)
    assert any("payload hash" in issue.message for issue in issues)


def test_axis_missing_payload_is_an_error(tmp_path: Path) -> None:
    root = axis_package(tmp_path / "pkg")
    (root / "payloads" / "9001-official-00.json").unlink()
    issues = tp.validate_package(tp.load_package(root))
    assert any(issue.code == "axis.payload_missing" for issue in issues)


def test_axis_payload_id_must_match_task_json(tmp_path: Path) -> None:
    root = axis_package(tmp_path / "pkg")
    payload_path = root / "payloads" / "9001-official-00.json"
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    payload["id"] = 9002
    payload_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    # The payload hash now differs too, but the id mismatch must be named.
    issues = tp.validate_package(tp.load_package(root))
    messages = " ".join(issue.message for issue in issues)
    assert "payload id" in messages


def test_axis_reference_must_exist(tmp_path: Path) -> None:
    root = axis_package(tmp_path / "pkg")
    (root / "reference.json").unlink()
    issues = tp.validate_package(tp.load_package(root))
    assert any(issue.code == "axis.reference_missing" for issue in issues)


# ── RoboDojo ─────────────────────────────────────────────────────────────────


def robodojo_package(root: Path, *, ref: str | None = None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    _write_json(
        root / "task.json",
        {
            "schema_version": 1,
            "source": "robodojo",
            "task_id": "cover_blocks",
            "title": "Cover the blocks",
            "instruction": "Cover the blocks.",
            "dimension": "memory",
            "official_task": "cover_blocks",
            "robodojo_ref": ref or tp.ROBODOJO_PINNED_REF,
            "instance_count": 50,
            "protocol": {"episodes": 50, "checker": "official _result.json success"},
        },
    )
    return root


def test_a_well_formed_robodojo_package_has_no_issues(tmp_path: Path) -> None:
    package = tp.load_package(robodojo_package(tmp_path / "pkg"))
    assert tp.validate_package(package) == []


def test_a_different_robodojo_pin_is_a_warning_not_an_error(tmp_path: Path) -> None:
    package = tp.load_package(robodojo_package(tmp_path / "pkg", ref="a" * 40))
    issues = tp.validate_package(package)
    warnings = [issue for issue in issues if issue.severity == "warning"]
    assert [issue.code for issue in warnings] == ["robodojo.pinned_ref_differs"]
    assert not any(issue.is_error for issue in issues)


def test_robodojo_requires_the_official_task_name(tmp_path: Path) -> None:
    root = robodojo_package(tmp_path / "pkg")
    task = json.loads((root / "task.json").read_text(encoding="utf-8"))
    task["official_task"] = "something_else"
    (root / "task.json").write_text(json.dumps(task), encoding="utf-8")
    issues = tp.validate_package(tp.load_package(root))
    assert any(issue.code == "robodojo.official_task_mismatch" for issue in issues)
