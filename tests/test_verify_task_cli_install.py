"""The candidate-install verifier must be fail-closed.

The acceptance question is "does the candidate wheel expose the shared digest
and does it agree with the CLI". A missing module/function, a non-zero exit,
malformed output or a non-64-hex value must be a failure. The previous version
recorded `available: false` and still exited 0, so a probe pointed at a
deliberately missing function stayed green.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
from types import ModuleType

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _script() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "verify_task_cli_install", ROOT / "scripts" / "verify_task_cli_install.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _completed(
    returncode: int, *, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["python"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def test_a_missing_module_or_function_is_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _script()
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *a, **k: _completed(
            1, stderr="ModuleNotFoundError: No module named 'missing'"
        ),
    )
    with pytest.raises(SystemExit, match="shared digest probe failed"):
        module.shared_digest(
            pathlib.Path("/venv/bin/python"),
            "/package",
            "openroboto_protocol.package_digest",
            "missing_required_digest",
        )


def test_malformed_output_is_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _script()
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *a, **k: _completed(0, stdout='{"digest": "not-a-sha256"}'),
    )
    with pytest.raises(SystemExit, match="64-character"):
        module.shared_digest(
            pathlib.Path("/venv/bin/python"),
            "/package",
            "openroboto_protocol.package_digest",
            "package_digest",
        )


def test_uppercase_digest_is_not_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _script()
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *a, **k: _completed(0, stdout=json.dumps({"digest": "A" * 64})),
    )
    with pytest.raises(SystemExit, match="64-character"):
        module.shared_digest(
            pathlib.Path("/venv/bin/python"),
            "/package",
            "openroboto_protocol.package_digest",
            "package_digest",
        )


def test_a_valid_digest_is_returned(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _script()
    digest = "a" * 64
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *a, **k: _completed(0, stdout=json.dumps({"digest": digest})),
    )
    assert (
        module.shared_digest(
            pathlib.Path("/venv/bin/python"),
            "/package",
            "openroboto_protocol.package_digest",
            "package_digest",
        )
        == digest
    )
