"""Detached, minimal receipts for completed task checks before payment."""

from __future__ import annotations

import hashlib
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openroboto.config import ConfigError, Settings
from openroboto.task_runtime import runtime_release
from openroboto.task_workspace import json_text, workspace_path

RECEIPT_FILE = "check-receipt.json"
RECEIPT_SCHEMA = 1


def receipt_path(settings: Settings, config: str) -> Path:
    return workspace_path(settings, config, "evidence") / RECEIPT_FILE


def workspace_identity(config: str) -> str:
    return hashlib.sha256(
        str(Path(config).expanduser().resolve().parent).encode()
    ).hexdigest()


def competition_identity(settings: Settings) -> dict[str, Any]:
    policy = settings.competition_params.get("task", {})
    return {
        "backend": settings.backend_url.rstrip("/"),
        "track": settings.competition["track"],
        "seq": settings.competition["seq"],
        "source": policy.get("source"),
        "execution_profile": policy.get("execution_profile"),
    }


def invalidate_receipt(settings: Settings, config: str, package: Path) -> None:
    target = receipt_path(settings, config)
    if target.resolve().is_relative_to(package.resolve()):
        raise ConfigError(
            "Keep the check receipt in evidence/ outside the task package."
        )
    target.unlink(missing_ok=True)


def write_receipt(
    settings: Settings,
    config: str,
    result: dict[str, Any],
    *,
    scope: str,
) -> Path | None:
    """Project only identities and check booleans, never private diagnostics."""
    digest = result.get("package_digest")
    if not result.get("ok") or not digest:
        return None
    checks = result.get("checks") or [
        {"name": "package.format", "passed": True, "kind": "structural"}
    ]
    if scope == "full" and (
        not result["ready_to_submit"]
        or not any(c["kind"] == "runtime" for c in checks)
        or not all(c["passed"] for c in checks)
    ):
        raise ConfigError(
            "A full receipt requires successful runtime checks. "
            "Run openroboto check again."
        )
    release = runtime_release()
    receipt = {
        "schema_version": RECEIPT_SCHEMA,
        "package_digest": digest,
        "checked_at": datetime.now(UTC).isoformat(),
        "scope": scope,
        "accepted": True,
        "workspace_id": workspace_identity(config),
        "competition": competition_identity(settings),
        "admission_version": release["admission_version"],
        "admission_policy_sha256": release["admission_policy_sha256"],
        "runtime_version": release["version"],
        "runtime_content_sha256": release["content_sha256"],
        "image_digest": result["image_digest"] if scope == "full" else None,
        "checks": [
            {key: check[key] for key in ("name", "passed", "kind")} for check in checks
        ],
    }
    target = receipt_path(settings, config)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", dir=target.parent, delete=False
    ) as stream:
        stream.write(json_text(receipt))
    Path(stream.name).replace(target)
    return target
