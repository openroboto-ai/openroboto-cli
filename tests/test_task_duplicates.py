"""Duplicate check: advisory in `check`, a payment gate on `full` in `submit`."""

import json
from types import SimpleNamespace

import pytest

from openroboto import backend_api, task_duplicates
from openroboto.commands import submit, task_check

SCENE = SimpleNamespace(
    competition_params={"task": {"execution_profile": {"id": "panda-open-scene-v1"}}},
    backend_url="https://backend.invalid",
)
VERDICTS = {
    "ok": {"verdict": "ok", "cap": 3, "same_problem": 0, "query_problem": "p"},
    "crowded": {
        "verdict": "crowded",
        "cap": 3,
        "same_problem": 2,
        "query_problem": "p",
    },
    "full": {"verdict": "full", "cap": 3, "same_problem": 3, "query_problem": "p"},
}


def backend(monkeypatch, outcome):
    def query(settings, package):
        if outcome == "unavailable":
            raise task_duplicates.DuplicateCheckUnavailable("HTTP 503")
        return dict(VERDICTS[outcome])

    monkeypatch.setattr(task_duplicates, "query", query)


@pytest.mark.parametrize("outcome", ["ok", "crowded", "full", "unavailable"])
def test_check_reports_the_verdict_and_never_fails(monkeypatch, tmp_path, outcome):
    backend(monkeypatch, outcome)
    result = task_check.duplicate_check(SCENE, tmp_path)
    assert result["verdict"] == outcome
    expected = {
        "ok": "no task in the pool",
        "crowded": "2 of 3 slots",
        "full": "fee is not refunded",
        "unavailable": "submit asks again before paying",
    }[outcome]
    assert expected in result["message"]


def test_check_skips_non_scene_tasks(monkeypatch, tmp_path):
    backend(monkeypatch, "full")
    assert (
        task_check.duplicate_check(SimpleNamespace(competition_params={}), tmp_path)
        == {}
    )


@pytest.mark.parametrize(
    "outcome, allow, payable",
    [
        ("ok", False, True),
        ("crowded", False, True),
        ("full", False, False),
        ("full", True, True),
        ("unavailable", False, True),
    ],
)
def test_submit_refuses_to_pay_only_for_full(
    monkeypatch, tmp_path, capsys, outcome, allow, payable
):
    backend(monkeypatch, outcome)
    assert submit.duplicate_is_payable(SCENE, tmp_path, allow) is payable
    out = capsys.readouterr()
    if not payable:
        assert "--allow-duplicate" in out.out + out.err


def test_query_signs_the_access_message_and_sends_execution_task(monkeypatch, tmp_path):
    (tmp_path / "execution").mkdir()
    (tmp_path / "execution" / "task.json").write_text(json.dumps({"task_id": "t"}))
    sent = {}
    wallet = SimpleNamespace(hotkey=SimpleNamespace(ss58_address="5Hot"))
    monkeypatch.setattr("openroboto.chain.open_wallet", lambda settings: wallet)
    monkeypatch.setattr(
        "openroboto.chain.hotkey_signature", lambda w, msg: f"sig({msg.decode()})"
    )
    monkeypatch.setattr(
        "openroboto.competition.workspace_competition_id", lambda settings: 17
    )
    monkeypatch.setattr(
        task_duplicates,
        "fetch_task_access_message",
        lambda url, hotkey: f"msg:{hotkey}",
    )
    monkeypatch.setattr(task_duplicates, "miner_key", lambda settings: "k")

    def check(url, **kwargs):
        sent.update(kwargs)
        return dict(VERDICTS["ok"])

    monkeypatch.setattr(task_duplicates, "check_task_duplicates", check)
    assert task_duplicates.query(SCENE, tmp_path)["verdict"] == "ok"
    assert sent == {
        "hotkey": "5Hot",
        "signature": "sig(msg:5Hot)",
        "task": {"task_id": "t"},
        "competition_id": 17,
        "miner_key": "k",
    }


def test_query_failures_are_unavailable(monkeypatch, tmp_path):
    with pytest.raises(task_duplicates.DuplicateCheckUnavailable):
        task_duplicates.query(SCENE, tmp_path)  # no execution/task.json


def test_api_rejects_an_unknown_verdict(monkeypatch):
    monkeypatch.setattr(
        backend_api,
        "_post",
        lambda *a, **k: json.dumps({"data": {"verdict": "maybe"}}).encode(),
    )
    with pytest.raises(backend_api.BackendError):
        backend_api.check_task_duplicates(
            "u", hotkey="h", signature="s", task={}, competition_id=None
        )
    monkeypatch.setattr(
        backend_api,
        "_post",
        lambda *a, **k: json.dumps({"data": VERDICTS["full"]}).encode(),
    )
    assert (
        backend_api.check_task_duplicates(
            "u", hotkey="h", signature="s", task={}, competition_id=1
        )["verdict"]
        == "full"
    )
