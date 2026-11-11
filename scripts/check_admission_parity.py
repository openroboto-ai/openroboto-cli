"""Compare pinned worker/CLI admission evidence without changing verdicts.

Version checks happen before any case comparison. Only bundles produced by the
same admission implementation and fixture suite may be compared. Locator
normalization belongs to the producer; this comparator never rewrites details.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

IDENTITY_FIELDS = (
    "validator_commit",
    "admission_version",
    "admission_source_sha256",
    "fixture_suite_sha256",
)
MISSING = {"missing": True}


def difference(field: str, expected: Any, actual: Any) -> dict[str, Any]:
    return {"field": field, "expected": expected, "actual": actual}


def compare_value(expected: Any, actual: Any, field: str) -> list[dict[str, Any]]:
    if type(expected) is not type(actual):
        return [difference(field, expected, actual)]
    if isinstance(expected, dict):
        differences = []
        for key in sorted(expected.keys() | actual.keys()):
            location = f"{field}.{key}" if field else key
            if key not in expected:
                differences.append(difference(location, MISSING, actual[key]))
            elif key not in actual:
                differences.append(difference(location, expected[key], MISSING))
            else:
                differences.extend(compare_value(expected[key], actual[key], location))
        return differences
    if isinstance(expected, list):
        if len(expected) != len(actual):
            return [difference(field, expected, actual)]
        differences = []
        for index, (left, right) in enumerate(zip(expected, actual, strict=True)):
            differences.extend(compare_value(left, right, f"{field}[{index}]"))
        return differences
    return [] if expected == actual else [difference(field, expected, actual)]


def _results(bundle: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows = bundle["results"]
    if not isinstance(rows, list) or not rows:
        raise ValueError("results must be a non-empty array")
    result = {}
    for row in rows:
        name = row["case"]
        if not isinstance(name, str) or not name or name in result:
            raise ValueError(f"case names must be unique non-empty strings: {name!r}")
        if not isinstance(row["package_digest"], str):
            raise ValueError(f"{name}.package_digest must be a string")
        if type(row["exit_code"]) is not int:
            raise ValueError(f"{name}.exit_code must be an integer")
        verdict = row["verdict"]
        if verdict["code"] is not None and not isinstance(verdict["code"], str):
            raise ValueError(f"{name}.code must be a string or null")
        if type(verdict["accepted"]) is not bool:
            raise ValueError(f"{name}.accepted must be a boolean")
        checks = verdict["checks"]
        if not isinstance(checks, list):
            raise ValueError(f"{name}.checks must be an array")
        names = []
        for check in checks:
            if set(check) != {"name", "passed", "kind", "detail"}:
                raise ValueError(f"{name}: each check needs name/passed/kind/detail")
            if not isinstance(check["name"], str) or not check["name"]:
                raise ValueError(f"{name}: check names must be non-empty strings")
            if not isinstance(check["kind"], str) or not check["kind"]:
                raise ValueError(f"{name}: check kinds must be non-empty strings")
            if not isinstance(check["detail"], dict):
                raise ValueError(f"{name}.{check['name']}.detail must be an object")
            if type(check["passed"]) is not bool:
                raise ValueError(f"{name}.{check['name']}.passed must be a boolean")
            names.append(check["name"])
        if len(names) != len(set(names)):
            raise ValueError(f"{name}: duplicate check names")
        result[name] = row
    return result


def compare_case(
    expected: dict[str, Any], actual: dict[str, Any]
) -> list[dict[str, Any]]:
    differences = []
    for field in ("package_digest", "exit_code"):
        differences.extend(
            compare_value(expected[field], actual.get(field, MISSING), field)
        )
    left, right = expected["verdict"], actual["verdict"]
    for field in ("accepted", "code"):
        differences.extend(compare_value(left[field], right.get(field, MISSING), field))
    expected_checks = {c["name"]: c for c in left["checks"]}
    actual_checks = {c["name"]: c for c in right["checks"]}
    for name in sorted(expected_checks.keys() | actual_checks.keys()):
        field = f"checks[{name}]"
        if name not in expected_checks:
            differences.append(difference(field, MISSING, actual_checks[name]))
        elif name not in actual_checks:
            differences.append(difference(field, expected_checks[name], MISSING))
        else:
            differences.extend(
                compare_value(expected_checks[name], actual_checks[name], field)
            )
    if set(expected_checks) == set(actual_checks):
        order_left = [c["name"] for c in left["checks"]]
        order_right = [c["name"] for c in right["checks"]]
        differences.extend(compare_value(order_left, order_right, "checks.order"))
    return differences


def compare_bundles(expected: dict[str, Any], actual: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(expected, dict) or not isinstance(actual, dict):
        return {
            "schema_version": 1,
            "status": "invalid_input",
            "error": "Each evidence bundle must be a JSON object",
        }
    result: dict[str, Any] = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "suite": expected.get("suite"),
    }
    try:
        identity_left, identity_right = expected["identity"], actual["identity"]
        if set(identity_left) != set(IDENTITY_FIELDS) or set(identity_right) != set(
            IDENTITY_FIELDS
        ):
            raise ValueError(
                "identity must name the exact worker commit, admission version, "
                "source and fixture digests"
            )
        versions = compare_value(identity_left, identity_right, "identity")
        if versions:
            return {**result, "status": "version_mismatch", "differences": versions}
        result["identity"] = identity_left
        for field in ("schema_version", "suite", "scope"):
            changes = compare_value(expected[field], actual.get(field, MISSING), field)
            if changes:
                return {**result, "status": "contract_mismatch", "differences": changes}
        rows_left, rows_right = _results(expected), _results(actual)
        cases = []
        for name in sorted(rows_left.keys() | rows_right.keys()):
            if name not in rows_left:
                changes = [difference("case", MISSING, rows_right[name])]
            elif name not in rows_right:
                changes = [difference("case", rows_left[name], MISSING)]
            else:
                changes = compare_case(rows_left[name], rows_right[name])
            cases.append({"case": name, "matched": not changes, "differences": changes})
        return {
            **result,
            "status": "matched"
            if all(c["matched"] for c in cases)
            else "verdict_mismatch",
            "results": cases,
        }
    except (KeyError, TypeError, ValueError) as exc:
        return {**result, "status": "invalid_input", "error": str(exc)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        baseline = json.loads(args.baseline.read_text())
        candidate = json.loads(args.candidate.read_text())
        result = compare_bundles(baseline, candidate)
    except (OSError, ValueError) as exc:
        result = {"schema_version": 1, "status": "invalid_input", "error": str(exc)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    for row in result.get("results", []):
        for change in row["differences"]:
            print(
                f"{row['case']}: {change['field']}: "
                f"worker={change['expected']!r}, cli={change['actual']!r}",
                file=sys.stderr,
            )
    if result["status"] == "version_mismatch":
        print(
            "Admission version mismatch; no case comparison was run.", file=sys.stderr
        )
        for change in result["differences"]:
            print(
                f"{change['field']}: worker={change['expected']!r}, "
                f"cli={change['actual']!r}",
                file=sys.stderr,
            )
    elif result["status"] != "matched":
        print(
            f"Admission parity failed: {result['status']}; see {args.output}",
            file=sys.stderr,
        )
    else:
        print(f"Matched {len(result['results'])} admission verdicts; see {args.output}")
    return 0 if result["status"] == "matched" else 1


if __name__ == "__main__":
    raise SystemExit(main())
