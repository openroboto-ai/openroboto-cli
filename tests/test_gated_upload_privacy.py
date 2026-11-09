"""Exercise submission ordering with a stateful HF API, never a real account."""

import argparse
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from huggingface_hub.hf_api import AccessRequest
from huggingface_hub.utils import HfHubHTTPError, RepositoryNotFoundError

from openroboto.commands import submit as command
from openroboto.competition_state import save_state
from openroboto.huggingface import access
from tests.test_submission_flow import (
    GRANT_TO,
    SEASON_ID,
    _gated_season,
    _miner_settings,
    _submitting,
    _uploaded_state,
    _verdict,
)


class Missing(RepositoryNotFoundError):
    def __init__(self) -> None:
        Exception.__init__(self, "missing")


class Refused(HfHubHTTPError):
    def __init__(self) -> None:
        Exception.__init__(self, "refused")


class Hub:
    """The accepted ACL and gating metadata are independent observable state."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.exists = False
        self.private = False
        self.gated: str | bool = False
        self.accepted: set[str] = set()
        self.fail_at = ""
        self.confirm_gate = True
        self.confirm_grant = True

    def repo_info(self, **kwargs: Any) -> Any:
        self.calls.append("info")
        if not self.exists:
            raise Missing()
        return SimpleNamespace(private=self.private, gated=self.gated)

    def create_repo(self, **kwargs: Any) -> None:
        assert kwargs.get("private", False) is False
        self.calls.append("create-empty")
        self.exists = True

    def update_repo_settings(self, **kwargs: Any) -> None:
        self.calls.append("gate")
        if self.fail_at == "gate":
            raise Refused()
        assert kwargs == {"repo_id": "owner/package", "gated": "manual"}
        if self.confirm_gate:
            self.gated = "manual"

    def list_accepted_access_requests(self, repo_id: str) -> list[Any]:
        self.calls.append("acl")
        if self.fail_at == "acl":
            raise Refused()
        return [
            AccessRequest(
                username=user,
                fullname="Test account",
                email=None,
                timestamp=datetime(2026, 11, 9, tzinfo=UTC),
                status="accepted",
            )
            for user in sorted(self.accepted)
        ]

    def grant_access(self, **kwargs: Any) -> None:
        self.calls.append("grant:" + kwargs["user"])
        if self.fail_at == "grant":
            raise Refused()
        if self.confirm_grant:
            self.accepted.add(kwargs["user"])


def setup_submission(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[Any, Hub, list[Any]]:
    monkeypatch.chdir(tmp_path)
    settings = _miner_settings()
    settings.hf_repo_id = "owner/package"
    hub = Hub()
    monkeypatch.setattr(access, "HfApi", lambda **kwargs: hub)
    _, paid = _submitting(monkeypatch, settings, verdict=_gated_season())

    def upload(settings: Any, cid: int, output: Any, state: Any, **kwargs: Any) -> None:
        hub.calls.append("upload")
        state.update(_uploaded_state())
        state["hf_repo_id"] = "owner/package"

    monkeypatch.setattr(command, "perform_upload", upload)
    return settings, hub, paid


def run() -> int:
    return command.run(
        argparse.Namespace(config="miner.yaml", output_dir="", force=False)
    )


def test_new_repository_is_gated_and_granted_before_payload_or_payment(
    monkeypatch: Any, tmp_path: Path
) -> None:
    _, hub, paid = setup_submission(monkeypatch, tmp_path)
    assert run() == 0
    assert hub.calls.index("create-empty") < hub.calls.index("gate")
    assert (
        hub.calls.index("gate")
        < hub.calls.index("grant:" + GRANT_TO)
        < hub.calls.index("upload")
    )
    assert hub.accepted == {GRANT_TO}
    assert paid == [0.25]
    # A second live check and ACL check still protect the eventual payment.
    assert hub.calls.count("gate") == 2


@pytest.mark.parametrize(
    "failure",
    [
        "gate",
        "acl",
        "grant",
        "unconfirmed-gate",
        "unconfirmed-grant",
        "private",
        "unexpected",
    ],
)
def test_access_failure_never_uploads_or_pays(
    monkeypatch: Any, tmp_path: Path, failure: str
) -> None:
    _, hub, paid = setup_submission(monkeypatch, tmp_path)
    hub.exists = True
    hub.fail_at = failure
    hub.confirm_gate = failure != "unconfirmed-gate"
    hub.confirm_grant = failure != "unconfirmed-grant"
    hub.private = failure == "private"
    hub.accepted = {"unrelated-user"} if failure == "unexpected" else set()
    assert run() == 1
    assert "upload" not in hub.calls
    assert paid == []
    if failure == "private":
        assert "gate" not in hub.calls
        assert hub.private
    if failure == "unexpected":
        assert hub.accepted == {"unrelated-user"}
        assert not any(call.startswith("grant:") for call in hub.calls)


@pytest.mark.parametrize("live_grant", ["", "another-evaluator"])
def test_snapshot_privacy_is_not_silently_relaxed_or_retargeted(
    monkeypatch: Any, tmp_path: Path, live_grant: str
) -> None:
    settings, hub, paid = setup_submission(monkeypatch, tmp_path)
    settings.competition["params"]["hf"] = {"grant_to": GRANT_TO}
    verdict = _gated_season()
    verdict.live.params["hf"]["grant_to"] = live_grant
    monkeypatch.setattr(command, "resolve_competition", lambda *args: verdict)
    assert run() == 1
    assert hub.calls == []
    assert paid == []


def test_live_enabling_overrides_a_snapshot_without_gating(
    monkeypatch: Any, tmp_path: Path
) -> None:
    settings, hub, paid = setup_submission(monkeypatch, tmp_path)
    assert "hf" not in settings.competition["params"]
    assert run() == 0
    assert hub.calls.index("grant:" + GRANT_TO) < hub.calls.index("upload")
    assert paid == [0.25]


def test_account_change_during_upload_refuses_payment(
    monkeypatch: Any, tmp_path: Path
) -> None:
    _, hub, paid = setup_submission(monkeypatch, tmp_path)
    second = _gated_season()
    second.live.params["hf"]["grant_to"] = "new-evaluator"
    verdicts = iter([_gated_season(), second])
    monkeypatch.setattr(command, "resolve_competition", lambda *args: next(verdicts))
    assert run() == 1
    assert "upload" in hub.calls
    assert paid == []
    assert "grant:new-evaluator" not in hub.calls


def test_no_grant_season_leaves_hf_permissions_alone(
    monkeypatch: Any, tmp_path: Path
) -> None:
    _, hub, paid = setup_submission(monkeypatch, tmp_path)
    monkeypatch.setattr(command, "resolve_competition", lambda *args: _verdict())
    assert run() == 0
    assert hub.calls == ["upload"]
    assert paid == [0.25]


@pytest.mark.parametrize("announced", [False, True])
def test_paid_or_announced_recovery_never_rechecks_access_or_pays(
    monkeypatch: Any, tmp_path: Path, announced: bool
) -> None:
    _, hub, paid = setup_submission(monkeypatch, tmp_path)
    state = _uploaded_state()
    state["burn_tx_hash"] = "paid"
    if announced:
        state["step"] = "announce"
    save_state(SEASON_ID, state)
    monkeypatch.setattr(
        command,
        "resolve_competition",
        lambda *args: pytest.fail("paid recovery must not recheck"),
    )
    monkeypatch.setattr(command, "perform_upload", lambda *args, **kwargs: None)
    assert run() == 0
    assert hub.calls == []
    assert paid == []


def test_owner_does_not_need_an_explicit_grant(monkeypatch: Any) -> None:
    hub = Hub()
    hub.exists = True
    monkeypatch.setattr(access, "HfApi", lambda **kwargs: hub)
    access.prepare_gated_repository(
        repo_id="owner/package", grant_to="OWNER", hf_token="fake"
    )
    assert hub.gated == "manual"
    assert hub.accepted == set()
    assert "grant:OWNER" not in hub.calls


def test_new_approval_after_upload_refuses_payment_without_revoking(
    monkeypatch: Any, tmp_path: Path
) -> None:
    _, hub, paid = setup_submission(monkeypatch, tmp_path)
    upload = command.perform_upload

    def add_approval(*args: Any, **kwargs: Any) -> None:
        upload(*args, **kwargs)
        hub.accepted.add("unexpected-late-reader")

    monkeypatch.setattr(command, "perform_upload", add_approval)
    assert run() == 1
    assert "upload" in hub.calls
    assert paid == []
    assert "unexpected-late-reader" in hub.accepted


def test_acl_with_unreadable_account_refuses_access(monkeypatch: Any) -> None:
    hub = Hub()
    hub.exists = True
    monkeypatch.setattr(access, "HfApi", lambda **kwargs: hub)
    monkeypatch.setattr(
        hub,
        "list_accepted_access_requests",
        lambda repo_id: [
            AccessRequest(
                username="",
                fullname="Test account",
                email=None,
                timestamp=datetime(2026, 11, 9, tzinfo=UTC),
                status="accepted",
            )
        ],
    )
    with pytest.raises(access.AccessError, match="unreadable"):
        access.prepare_gated_repository(
            repo_id="owner/package", grant_to=GRANT_TO, hf_token="fake"
        )
    assert not any(call.startswith("grant:") for call in hub.calls)


def test_real_upload_pipeline_only_sends_files_after_confirmed_gating(
    monkeypatch: Any, tmp_path: Path
) -> None:
    import huggingface_hub

    from openroboto.commands.upload import perform_upload
    from openroboto.competition_state import load_state

    _, hub, paid = setup_submission(monkeypatch, tmp_path)
    folder = tmp_path / "payload"
    folder.mkdir()
    (folder / "model.safetensors").write_bytes(b"local-test-payload")
    monkeypatch.setattr(command, "perform_upload", perform_upload)
    monkeypatch.setattr(huggingface_hub, "HfApi", lambda **kwargs: hub)
    monkeypatch.setattr(huggingface_hub, "create_repo", hub.create_repo)

    def send_files(**kwargs: Any) -> str:
        assert hub.exists and hub.gated == "manual"
        assert hub.accepted == {GRANT_TO}
        sent_folder = Path(kwargs["folder_path"])
        assert (sent_folder / "model.safetensors").read_bytes() == b"local-test-payload"
        hub.calls.append("upload-files")
        return "https://huggingface.co/owner/package/commit/" + "a" * 40

    monkeypatch.setattr(huggingface_hub, "upload_folder", send_files)
    args = argparse.Namespace(config="miner.yaml", output_dir=str(folder), force=False)
    assert command.run(args) == 0
    assert hub.calls.index("grant:" + GRANT_TO) < hub.calls.index("upload-files")
    checkpoint = load_state(SEASON_ID)
    assert checkpoint["hf_repo_id"] == "owner/package"
    assert checkpoint["hf_commit"] == "a" * 40
    assert paid == [0.25]


@pytest.mark.parametrize("error", [KeyError, AttributeError, TypeError, RuntimeError])
def test_sdk_access_list_failure_is_controlled_and_never_uploads(
    monkeypatch: Any, tmp_path: Path, capsys: Any, error: type[Exception]
) -> None:
    _, hub, paid = setup_submission(monkeypatch, tmp_path)

    def broken_sdk(repo_id: str) -> Any:
        raise error("private-request-email@example.invalid")

    monkeypatch.setattr(hub, "list_accepted_access_requests", broken_sdk)
    assert run() == 1
    assert "upload" not in hub.calls
    assert paid == []
    err = capsys.readouterr().err
    assert "Could not verify the accepted access list" in err
    assert "SDK compatibility" in err
    assert "private-request-email" not in err
    assert "Traceback" not in err


def test_unknown_sdk_request_shape_is_rejected_without_attribute_error(
    monkeypatch: Any,
) -> None:
    hub = Hub()
    monkeypatch.setattr(
        hub, "list_accepted_access_requests", lambda repo_id: [object()]
    )
    with pytest.raises(access.AccessError, match="unreadable account"):
        access._check_accepted_accounts(hub, "owner/package", GRANT_TO)
