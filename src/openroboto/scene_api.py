"""The P04 scene HTTP endpoints, separate from chain/payment wire formats.

Registration messages follow backend 3be81bc8937f88c9 (unchanged from c9dfae2).
The protocol owns submission/result schemas; the backend owns HTTP auth and
canonical HTTP signature messages. No private bytes are sent in JSON requests.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from urllib.parse import quote

from openroboto_protocol.scene_contract import CapabilityStatus, parse_public_summary
from openroboto_protocol.scene_schema import SceneAdmissionResult, SubmissionManifest
from openroboto_protocol.schemas import Contract, Envelope
from pydantic import field_validator

from openroboto.backend_api import _get, _parse, _post


class SceneFinding(Contract):
    code: str
    path: str
    message: str


class SceneCapability(Contract):
    status: CapabilityStatus
    issues: list[SceneFinding]


class SceneEvent(Contract):
    stage: str
    code: str
    attempt: int | None
    at: datetime


class SceneProgress(Contract):
    task_id: str
    result: SceneAdmissionResult | None
    current_capability: SceneCapability
    summary: dict[str, Any] | None
    events: list[SceneEvent]

    @field_validator("summary")
    @classmethod
    def authored_summary_only(
        cls, value: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        return parse_public_summary(value) if value is not None else None


class SceneCatalog(Contract):
    profiles: list[dict[str, Any]]


def fetch_scene_profiles(base_url: str) -> list[dict[str, Any]]:
    path = "/api/v1/tasks/scene-profiles"
    return _parse(Envelope[SceneCatalog], _get(base_url, path), path).data.profiles


def registration_message(
    task_id: str, repo: str, commit: str, manifest: SubmissionManifest
) -> bytes:
    return json.dumps(
        {
            "domain": "openroboto:scene-registration:v1",
            "task_id": task_id,
            "package_repo": repo,
            "package_commit": commit,
            "submission": manifest.model_dump(mode="json"),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()


def fetch_scene_progress(base_url: str, task_id: str) -> SceneProgress:
    path = f"/api/v1/tasks/{quote(task_id, safe='')}/scene-progress"
    return _parse(Envelope[SceneProgress], _get(base_url, path), path).data


def register_scene(
    base_url: str,
    task_id: str,
    manifest: SubmissionManifest,
    signature: str,
    key: str,
) -> SceneProgress:
    path = f"/api/v1/tasks/{quote(task_id, safe='')}/scene-registration"
    raw = _post(
        base_url,
        path,
        {"submission": manifest.model_dump(mode="json"), "signature": signature},
        api_key=key,
    )
    return _parse(Envelope[SceneProgress], raw, path).data
