"""The doctor's protocol-capability check.

The version pin cannot tell whether a release contains both `Track.TASK` and
`AXIS_LAYOUT`; those landed on diverged lines. A task workspace must fail
before a formal run if the installed protocol cannot parse `track=task`; a
model workspace must not be failed for a track it does not mine.
"""

from __future__ import annotations

import sys

import openroboto_protocol
import openroboto_protocol.commitment as commitment
import pytest

from openroboto.commands.doctor import check_protocol_capabilities
from openroboto.config import Settings


def _settings(track: str) -> Settings:
    return Settings.from_mapping(
        {
            "competition": {
                "id": 4,
                "track": track,
                "seq": 1,
                "label": "Test season",
                "params": {},
            }
        }
    )


def test_a_task_workspace_fails_when_track_task_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Simulated rather than skipped: the pinned release has both capabilities,
    # and the refusal path still has to be exercised for older installs.
    monkeypatch.setattr(commitment, "Track", type("Track", (), {}))
    monkeypatch.setitem(sys.modules, "openroboto_protocol.package_digest", None)
    monkeypatch.delattr(openroboto_protocol, "package_digest", raising=False)

    result = check_protocol_capabilities(_settings("task"))
    assert result.ok is False
    assert result.required is True
    assert "Track.TASK=no" in result.detail
    assert "package_digest=no" in result.detail
    assert "exact protocol release" in result.fix
    assert "cherry-pick" in result.fix


def test_a_model_workspace_is_not_failed_for_a_track_it_does_not_mine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(commitment, "Track", type("Track", (), {}))

    result = check_protocol_capabilities(_settings("sim"))
    assert result.ok is True
    assert result.required is False
    assert "Track.TASK=no" in result.detail


def test_a_task_workspace_passes_once_the_protocol_has_both(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import types

    class _TrackWithTask:
        TASK = "task"

    fake = types.ModuleType("openroboto_protocol.package_digest")
    fake.package_digest = lambda directory: "digest"  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "openroboto_protocol.package_digest", fake)
    monkeypatch.setattr(openroboto_protocol, "package_digest", fake, raising=False)
    monkeypatch.setattr(commitment, "Track", _TrackWithTask)

    result = check_protocol_capabilities(_settings("task"))
    assert result.ok is True
    assert result.required is True
    assert "Track.TASK=yes" in result.detail
    assert "package_digest=yes" in result.detail
