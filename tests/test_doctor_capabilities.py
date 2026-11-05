"""The doctor's protocol-capability check.

The version pin cannot tell whether a release contains both `Track.TASK` and
`AXIS_LAYOUT`; those landed on diverged lines. A task workspace must fail
before a formal run if the installed protocol cannot parse `track=task`; a
model workspace must not be failed for a track it does not mine.
"""

from __future__ import annotations

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


def test_a_task_workspace_fails_when_track_task_is_missing() -> None:
    if getattr(commitment.Track, "TASK", None) is not None:
        pytest.skip("the installed protocol already knows Track.TASK")

    result = check_protocol_capabilities(_settings("task"))
    assert result.ok is False
    assert result.required is True
    assert "Track.TASK=no" in result.detail
    assert "61c1642" in result.fix
    assert "443fdbb" in result.fix
    assert "component verification only" in result.fix


def test_a_model_workspace_is_not_failed_for_a_track_it_does_not_mine() -> None:
    if getattr(commitment.Track, "TASK", None) is not None:
        pytest.skip("the installed protocol already knows Track.TASK")

    result = check_protocol_capabilities(_settings("sim"))
    assert result.ok is True
    assert result.required is False
    assert "Track.TASK=no" in result.detail


def test_a_task_workspace_passes_once_the_protocol_has_both(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _TrackWithTask:
        TASK = "task"

    monkeypatch.setattr(commitment, "Track", _TrackWithTask)
    result = check_protocol_capabilities(_settings("task"))
    assert result.ok is True
    assert result.required is True
    assert "Track.TASK=yes" in result.detail
