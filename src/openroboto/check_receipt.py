"""Detached, minimal receipts for completed task checks before payment."""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from collections.abc import Mapping
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
    if "submission_digest" in result:
        receipt.update(
            {
                key: result[key]
                for key in (
                    "submission_digest",
                    "private_package_digest",
                    "environment",
                    "execution_contract_digest",
                    "runtime_identity_digest",
                )
            }
        )
    target = receipt_path(settings, config)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", dir=target.parent, delete=False
    ) as stream:
        stream.write(json_text(receipt))
    Path(stream.name).replace(target)
    return target


def verify_receipt(
    settings: Settings,
    live: Any,
    digest: str,
    *,
    submission_digest: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Bind the remote artifact to a successful check and live season policy."""
    path = receipt_path(settings, settings.config_path)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(
            f"No successful check receipt at {path}. "
            "Run openroboto check before submitting."
        ) from exc
    except (OSError, ValueError) as exc:
        raise ConfigError(
            f"Cannot read check receipt {path}: {exc}. Run openroboto check again."
        ) from exc
    if (
        not isinstance(value, dict)
        or type(value.get("schema_version")) is not int
        or value["schema_version"] != RECEIPT_SCHEMA
    ):
        raise ConfigError("Invalid check receipt schema. Run openroboto check again.")
    if value.get("package_digest") != digest:
        raise ConfigError(
            "Check receipt digest differs from repo@commit. Run openroboto check "
            "on the uploaded package and upload the checked bytes."
        )
    policy = live.params.get("task", {})
    receipt_policy = policy.get("check_receipt", {})
    if not isinstance(receipt_policy, Mapping):
        raise ConfigError(
            "The season's params.task.check_receipt must be an object. "
            "Ask the operator to correct it; nothing was paid."
        )
    ttl = receipt_policy.get("max_age_seconds")
    if type(ttl) is not int or ttl <= 0:
        raise ConfigError(
            "The season must declare a positive "
            "params.task.check_receipt.max_age_seconds. Ask the operator "
            "to set the receipt validity period; nothing was paid."
        )
    allow_static = receipt_policy.get("allow_static_only", False)
    if type(allow_static) is not bool:
        raise ConfigError(
            "The season's check_receipt.allow_static_only must be true or false. "
            "Ask the operator to correct it."
        )
    scope = value.get("scope")
    if scope not in ("full", "static-only"):
        raise ConfigError(
            "Check receipt has no valid scope. Run openroboto check again."
        )
    if scope == "static-only" and not allow_static:
        raise ConfigError(
            "The check receipt is static-only. Run openroboto check "
            "without --static-only before submitting."
        )
    if value.get("workspace_id") != workspace_identity(settings.config_path):
        raise ConfigError(
            "This check receipt belongs to another workspace. "
            "Run openroboto check in this workspace."
        )
    expected_competition = {
        "backend": settings.backend_url.rstrip("/"),
        "track": str(live.track),
        "seq": live.seq,
        "source": policy.get("source"),
        "execution_profile": policy.get("execution_profile"),
    }
    if value.get("competition") != expected_competition:
        raise ConfigError(
            "This check receipt belongs to a different season, source or "
            "execution profile. Refresh the workspace and run openroboto check again."
        )
    if (
        submission_digest is not None
        and value.get("submission_digest") != submission_digest
    ):
        raise ConfigError(
            "The check receipt does not bind both scene roots. "
            "Run the matching full scene self-test before submitting."
        )
    release = runtime_release()
    runtime_policy = policy.get("runtime", {})
    if not isinstance(runtime_policy, Mapping):
        raise ConfigError(
            "The season's task.runtime must be an object. "
            "Ask the operator to correct it."
        )
    if (
        not isinstance(runtime_policy.get("admission_version"), str)
        or not runtime_policy["admission_version"]
    ):
        raise ConfigError(
            "The season must declare task.runtime.admission_version. "
            "Ask the operator to publish the active worker admission version."
        )
    for receipt_key, release_key, policy_key in (
        ("admission_version", "admission_version", "admission_version"),
        (
            "admission_policy_sha256",
            "admission_policy_sha256",
            "admission_policy_sha256",
        ),
        ("runtime_version", "version", "version"),
        ("runtime_content_sha256", "content_sha256", "content_sha256"),
    ):
        expected = runtime_policy.get(policy_key, release[release_key])
        if expected != release[release_key]:
            raise ConfigError(
                f"The season requires another {policy_key}. Install the season's "
                "CLI/runtime release and run openroboto check again."
            )
        if value.get(receipt_key) != expected:
            raise ConfigError(
                f"Check receipt {receipt_key} does not match the current "
                "admission/runtime version. Run openroboto check using "
                "this season's CLI release."
            )
    if scope == "full":
        image = value.get("image_digest")
        if not isinstance(image, str) or not re.fullmatch(
            r"sha256:[0-9a-f]{64}", image
        ):
            raise ConfigError(
                "Full check receipt has no immutable image digest. "
                "Run openroboto check again."
            )
        if "image_digest" in runtime_policy and image != runtime_policy["image_digest"]:
            raise ConfigError(
                "Check receipt image digest differs from the season runtime. "
                "Use the declared image and run openroboto check again."
            )
    checks = value.get("checks")
    if value.get("accepted") is not True or not isinstance(checks, list) or not checks:
        raise ConfigError(
            "The receipt does not record a successful check. "
            "Run openroboto check again."
        )
    for check in checks:
        if (
            not isinstance(check, dict)
            or set(check) != {"name", "passed", "kind"}
            or not isinstance(check["name"], str)
            or not check["name"]
            or check["passed"] is not True
            or check["kind"] not in ("structural", "runtime")
        ):
            raise ConfigError(
                "The receipt contains a failed or invalid check. "
                "Run openroboto check again."
            )
    if scope == "full" and not any(c["kind"] == "runtime" for c in checks):
        raise ConfigError(
            "The full receipt has no runtime checks. "
            "Run openroboto check without --static-only."
        )
    try:
        checked = datetime.fromisoformat(value["checked_at"])
        if checked.tzinfo is None or checked.utcoffset() is None:
            raise ValueError("timestamp has no timezone")
    except (KeyError, TypeError, ValueError) as exc:
        raise ConfigError(
            "Check receipt has no valid checked_at timestamp. "
            "Run openroboto check again."
        ) from exc
    age = ((now or datetime.now(UTC)) - checked).total_seconds()
    if age < 0:
        raise ConfigError(
            "Check receipt is dated in the future. Correct the system clock "
            "and run openroboto check again."
        )
    if age >= ttl:
        raise ConfigError(
            f"Check receipt expired (valid for {ttl} seconds). "
            "Run openroboto check again before submitting."
        )
    return value
