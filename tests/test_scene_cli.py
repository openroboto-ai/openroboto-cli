"""Synthetic contract fixtures and interface simulations, never physics evidence.

The input tree is copied verbatim from protocol aea37aa examples/open_scene/valid.
Its mesh has no runnable robot and its reference has no actions. Passing these
tests establishes only packaging, client routing and privacy boundaries.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from openroboto_protocol.scene_contract import profile_ref

from openroboto.cli import main
from openroboto.commands import submit, task_scene
from openroboto.config import Settings
from openroboto.scene_api import SceneProgress, registration_message
from openroboto.scene_package import build_submission, load_submission

FIXTURE = Path(__file__).parent / "fixtures/open_scene"


@pytest.fixture
def scene(tmp_path: Path) -> Path:
    root = tmp_path / "input"
    shutil.copytree(FIXTURE, root)
    return root


def read_output(capsys):
    return json.loads(capsys.readouterr().out)


def test_build_and_check_bind_both_roots_and_never_publish_reference(
    scene, tmp_path, capsys
):
    reference = tmp_path / "private-reference.json"
    reference.write_text(
        '{"schema_version":1,"actions":[],"private_marker":"do-not-log-me"}'
    )
    output = tmp_path / "submission"
    assert (
        main(
            [
                "task",
                "scene",
                "build",
                "--execution",
                str(scene / "execution"),
                "--reference",
                str(reference),
                "--output",
                str(output),
            ]
        )
        == 0
    )
    built = read_output(capsys)
    assert "do-not-log-me" not in json.dumps(built)
    assert built["scope"] == "structure_only"
    assert built["local_physics"] == built["server_admission"] == "not_run"
    assert main(["task", "scene", "check", str(output)]) == 0
    checked = read_output(capsys)
    assert built["submission"] == checked["submission"]
    assert checked["reward_status"] == "not_evaluated"
    assert "do-not-log-me" in (output / "private/reference.json").read_text()
    assert "do-not-log-me" not in (output / "execution/task.json").read_text()
    assert (
        main(
            [
                "task",
                "scene",
                "build",
                "--execution",
                str(scene / "execution"),
                "--private",
                str(scene / "private"),
                "--output",
                str(output),
            ]
        )
        == 1
    )
    assert read_output(capsys)["issues"][0]["code"] == "output_exists"


def test_build_from_private_tree_rebinds_execution_and_retains_stray_bytes(
    scene, tmp_path, capsys
):
    (scene / "execution/extra.txt").write_text("counts in E")
    (scene / "private/notes.txt").write_text("counts in P")
    output = tmp_path / "submission"
    assert (
        main(
            [
                "task",
                "scene",
                "build",
                "--execution",
                str(scene / "execution"),
                "--private",
                str(scene / "private"),
                "--output",
                str(output),
            ]
        )
        == 0
    )
    built = read_output(capsys)
    before = json.loads((scene / "submission.json").read_text())
    assert (
        built["submission"]["execution_package_digest"]
        != before["execution_package_digest"]
    )
    assert (
        built["submission"]["private_package_digest"]
        != before["private_package_digest"]
    )
    assert (output / "private/notes.txt").read_text() == "counts in P"
    assert (output / "private").stat().st_mode & 0o077 == 0


@pytest.mark.parametrize(
    "change,code",
    [
        ("missing", "dependency_missing"),
        ("drift", "dependency_drift"),
        ("private", "private_drift"),
        ("execution", "execution_drift"),
        ("symlink", "path_unsafe"),
        ("root-link", "root_invalid"),
        ("schema", "scene_invalid"),
        ("manifest", "binding_invalid"),
        ("public-card", "public_card_forbidden"),
        ("reference", "reference_schema"),
    ],
)
def test_diagnostics_before_upload_are_private_and_actionable(
    scene, tmp_path, capsys, change, code
):
    root = scene
    if change == "missing":
        (scene / "execution/assets/part.obj").unlink()
    if change == "drift":
        (scene / "execution/assets/part.obj").write_text("private-marker")
    if change == "private":
        (scene / "private/secret.txt").write_text("private-marker")
    if change == "execution":
        (scene / "execution/extra.txt").write_text("private-marker")
    if change == "symlink":
        (scene / "private/link").symlink_to(scene / "execution/task.json")
    if change == "root-link":
        root = tmp_path / "link"
        root.symlink_to(scene)
    if change == "schema":
        (scene / "execution/task.json").write_text('{"private-marker":1}')
    if change == "manifest":
        (scene / "submission.json").write_text("{}")
    if change == "public-card":
        (scene / "private/README.md").write_text("private-marker")
    if change == "reference":
        (scene / "private/reference.json").write_text(
            '{"schema_version":true,"private_marker":"private-marker"}'
        )
    assert main(["task", "scene", "check", str(root)]) == 1
    raw = capsys.readouterr()
    assert "private-marker" not in raw.out + raw.err
    finding = json.loads(raw.out)["issues"][0]
    assert finding["code"] == code
    assert finding["path"] and finding["message"]


def test_xml_edges_are_independently_checked_after_dependency_hashes(scene, capsys):
    import hashlib

    xml = scene / "execution/scene.xml"
    xml.write_text('<mujoco><include file="unlisted.xml"/></mujoco>')
    task_path = scene / "execution/task.json"
    task = json.loads(task_path.read_text())
    for dependency in task["scene"]["dependencies"]:
        if dependency["path"] == "scene.xml":
            dependency.update(
                sha256=hashlib.sha256(xml.read_bytes()).hexdigest(),
                size_bytes=xml.stat().st_size,
            )
    task_path.write_text(json.dumps(task))
    assert main(["task", "scene", "check", str(scene)]) == 1
    assert read_output(capsys)["issues"][0]["code"] == "dependency_undeclared"


def test_remote_check_requires_exact_locator_and_checks_joint_digest(
    scene, monkeypatch, capsys
):
    assert main(["task", "scene", "check", "--repo", "author/scene"]) == 1
    assert read_output(capsys)["issues"][0]["code"] == "locator_incomplete"
    monkeypatch.setattr(task_scene, "fetch_package", lambda *a, **k: scene)
    assert (
        main(["task", "scene", "check", "--repo", "author/scene", "--commit", "a" * 40])
        == 0
    )
    assert read_output(capsys)["ok"]
    assert (
        main(
            [
                "task",
                "scene",
                "check",
                str(scene),
                "--expect-submission-digest",
                "0" * 64,
            ]
        )
        == 1
    )
    assert read_output(capsys)["issues"][0]["code"] == "submission_drift"


@pytest.mark.parametrize("command", ["record-reference", "self-test"])
def test_missing_scene_runtime_never_runs_author_code_or_claims_physics(
    scene, tmp_path, capsys, monkeypatch, command
):
    script = tmp_path / "local.py"
    script.write_text("raise AssertionError('must not execute')")
    calls = []

    def probe(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(
            argv, 0, stdout=b"private-marker", stderr=b"private-marker"
        )

    monkeypatch.setattr(task_scene.subprocess, "run", probe)
    output = tmp_path / "diagnostic.json"
    argv = ["task", "scene", command, str(scene), "--output", str(output)]
    if command == "record-reference":
        argv += ["--script", str(script)]
    assert main(argv) == 1
    report = read_output(capsys)
    assert report["local_physics"] == "not_run"
    assert report["capability"] == "resource_unavailable"
    assert "private-marker" not in output.read_text()
    assert calls[0][-2:] == ["describe", "--include-development"]
    assert main(argv) == 1
    assert read_output(capsys)["issues"][0]["code"] == "report_path"


def test_preupload_and_remote_payment_gates_bind_both_roots(
    scene, tmp_path, monkeypatch
):
    policy = {"task": {"source": "axis", "execution_profile": profile_ref()}}
    state = {"hf_repo_id": "author/scene", "hf_commit": "a" * 40}
    assert submit._prepare_scene_submission(str(scene), policy, state)
    monkeypatch.setattr(submit, "fetch_package", lambda *a, **k: scene)
    monkeypatch.setattr(submit, "fetch_scene_profiles", lambda *a: [catalog(scene)])
    live = SimpleNamespace(params=policy)
    assert submit.task_package_is_payable(Settings(), state, live)
    assert (
        state["package_digest"]
        == load_submission(scene).manifest.execution_package_digest
    )
    assert submit.task_package_upload_is_unchanged(state, str(scene))
    # Even a self-consistent new private binding cannot replace the checked identity.
    (scene / "private/reference.json").write_text(
        '{"schema_version":1,"private_marker":"changed"}'
    )
    changed = build_submission(
        scene / "execution", tmp_path / "changed", private=scene / "private"
    )
    monkeypatch.setattr(submit, "fetch_package", lambda *a, **k: changed.root)
    assert not submit.task_package_is_payable(Settings(), state, live)
    assert not submit.task_package_upload_is_unchanged(state, str(scene))


def catalog(scene):
    task = load_submission(scene).task
    return {
        "execution_profile": profile_ref(),
        "capability_status": "supported",
        "available_for_submission": True,
        "verified_routes": [
            {
                "execution_profile": profile_ref(),
                "environment": task.environment.model_dump(),
                "robot": task.robot.artifact.model_dump(),
                "control_mapping": task.robot.control_mapping.model_dump(),
                "resource_profile": task.task_definition.resource_profile,
                "observation_contract": task.task_definition.observation,
            }
        ],
    }


@pytest.mark.parametrize(
    "change", ["development", "disabled", "environment", "observation", "malformed"]
)
def test_server_gate_requires_a_current_exact_route(scene, monkeypatch, change):
    profile = catalog(scene)
    if change == "development":
        profile["capability_status"] = "development"
    if change == "disabled":
        profile["available_for_submission"] = False
    if change == "malformed":
        profile["verified_routes"] = None
    if change in {"environment", "observation"}:
        profile["verified_routes"][0][
            "environment" if change == "environment" else "observation_contract"
        ] = "other"
    monkeypatch.setattr(submit, "fetch_scene_profiles", lambda *a: [profile])
    assert not submit._server_scene_ready(Settings(), load_submission(scene).task)


def progress(scene):
    return SceneProgress(
        task_id="backend-task",
        summary=None,
        events=[],
        current_capability={"status": "development", "issues": []},
        result={
            "schema_version": 1,
            "submission_digest": load_submission(scene).manifest.submission_digest,
            "capability": {
                "status": "development",
                "issues": [
                    {
                        "code": "SCENE_RUNTIME_NOT_REGISTERED",
                        "path": "runtime",
                        "message": "No registered runtime.",
                    }
                ],
            },
            "admission": "not_run",
            "reward_status": "not_evaluated",
            "evidence_digest": None,
        },
    )


def test_registration_signs_transport_and_roots_without_inferring_reward(
    scene, tmp_path, capsys, monkeypatch
):
    settings = Settings(backend_url="https://example.invalid", hotkey_ss58="author")
    monkeypatch.setattr(task_scene.Settings, "load", lambda *a: settings)
    monkeypatch.setattr(
        task_scene,
        "open_wallet",
        lambda *a: SimpleNamespace(hotkey=SimpleNamespace(ss58_address="author")),
    )
    monkeypatch.setattr(task_scene, "miner_key", lambda *a: "MINER-FIXTURE")
    monkeypatch.setattr(task_scene, "fetch_task_access_message", lambda *a: "access")
    row = SimpleNamespace(
        task_id="backend-task", package_repo="author/scene", package_commit="a" * 40
    )
    monkeypatch.setattr(
        task_scene,
        "fetch_my_tasks",
        lambda *a, **k: SimpleNamespace(
            data=[row],
            meta=SimpleNamespace(page=SimpleNamespace(offset=0, has_more=False)),
        ),
    )
    monkeypatch.setattr(task_scene, "fetch_package", lambda *a, **k: scene)
    messages = []

    def sign(wallet, message):
        messages.append(message)
        return "b" * 128

    monkeypatch.setattr(task_scene, "hotkey_signature", sign)
    monkeypatch.setattr(task_scene, "register_scene", lambda *a: progress(scene))
    assert (
        main(["task", "scene", "register", "backend-task", "--package", str(scene)])
        == 0
    )
    assert messages[-1] == registration_message(
        row.task_id,
        row.package_repo,
        row.package_commit,
        load_submission(scene).manifest,
    )
    registered = read_output(capsys)
    assert registered["result"]["admission"] == "not_run"
    assert registered["result"]["reward_status"] == "not_evaluated"
    monkeypatch.setattr(task_scene, "fetch_scene_progress", lambda *a: progress(scene))
    assert main(["task", "scene", "status", "backend-task"]) == 0
    assert read_output(capsys) == registered
    (scene / "private/extra.txt").write_text("changed")
    assert (
        main(["task", "scene", "register", "backend-task", "--package", str(scene)])
        == 1
    )
    assert read_output(capsys)["issues"][0]["code"] == "private_drift"


def test_registration_wallet_mismatch_fails_before_any_backend_request(
    scene, capsys, monkeypatch
):
    monkeypatch.setattr(
        task_scene.Settings, "load", lambda *a: Settings(hotkey_ss58="author")
    )
    monkeypatch.setattr(
        task_scene,
        "open_wallet",
        lambda *a: SimpleNamespace(hotkey=SimpleNamespace(ss58_address="other")),
    )

    def forbidden(*a):
        raise AssertionError("No request should be sent with the wrong wallet")

    monkeypatch.setattr(task_scene, "miner_key", forbidden)
    assert (
        main(["task", "scene", "register", "backend-task", "--package", str(scene)])
        == 1
    )
    assert read_output(capsys)["issues"][0]["code"] == "wallet_mismatch"


def test_scene_http_envelope_auth_and_safe_progress(scene, monkeypatch):
    import io

    from openroboto import backend_api, scene_api

    calls = []
    view = progress(scene).model_dump(mode="json")
    view["private_reference"] = "must-be-ignored"
    view["current_capability"]["private_reference"] = "must-be-ignored"

    def http(request, timeout):
        calls.append(request)
        assert request.headers["Accept"] == backend_api.ACCEPT
        return io.BytesIO(
            json.dumps({"data": view, "meta": {"request_id": "fixture"}}).encode()
        )

    monkeypatch.setattr(backend_api, "urlopen", http)
    manifest = load_submission(scene).manifest
    registered = scene_api.register_scene(
        "https://example.invalid", "backend-task", manifest, "a" * 128, "MINER-FIXTURE"
    )
    assert calls[-1].headers["X-api-key"] == "MINER-FIXTURE"
    sent = json.loads(calls[-1].data)
    assert set(sent) == {"submission", "signature"}
    assert sent["submission"] == manifest.model_dump()
    assert "must-be-ignored" not in json.dumps(registered.model_dump(mode="json"))
    status = scene_api.fetch_scene_progress("https://example.invalid", "backend-task")
    assert status == registered
    assert calls[-1].full_url.endswith("/api/v1/tasks/backend-task/scene-progress")


@pytest.mark.parametrize("command", ["record-reference", "self-test"])
def test_legacy_physics_commands_refuse_scene_roots(scene, monkeypatch, command):
    from openroboto.commands import task_authoring

    def forbidden(*a, **k):
        raise AssertionError("A v1 recorder must not receive a schema-3 package")

    monkeypatch.setattr(task_authoring, "_runtime", forbidden)
    argv = ["task", command, str(scene / "execution")]
    if command == "record-reference":
        argv.append("--example")
    assert main(argv) == 1


@pytest.mark.parametrize(
    "failure",
    [
        "none",
        "dependency",
        "account",
        "route",
        "changed-local",
        "paid-resume",
        "announced",
        "force-invalid",
    ],
)
def test_normal_submit_entry_orders_gates_and_reuses_paid_checkpoint(
    scene, tmp_path, monkeypatch, failure
):
    from openroboto.competition import Snapshot

    params = {
        "task": {"source": "axis", "execution_profile": profile_ref()},
        "hf": {"grant_to": "operator"},
    }
    settings = Settings(
        competition={
            "id": 77,
            "track": "task",
            "seq": 1,
            "label": "Scene fixture",
            "params": params,
        }
    )
    state = {}
    calls = []
    if failure == "dependency":
        (scene / "execution/assets/part.obj").unlink()
    if failure == "account":
        params.pop("hf")
    if failure in {"paid-resume", "announced", "force-invalid"}:
        state.update(
            scene_submission=load_submission(scene).manifest.model_dump(),
            scene_local_path=str(scene),
            burn_tx_hash="fixture-tx",
            burn_block=1,
            hf_repo_id="author/scene",
            hf_commit="a" * 40,
            hf_url="https://example.invalid",
        )
        if failure == "announced":
            state["step"] = "announce"
        if failure == "force-invalid":
            (scene / "private/extra.txt").write_text("changed")
    monkeypatch.setattr(submit.Settings, "load", lambda *a: settings)
    monkeypatch.setattr(
        submit, "load_snapshot", lambda *a: Snapshot(settings.competition)
    )
    monkeypatch.setattr(submit, "load_state", lambda *a: state)
    monkeypatch.setattr(submit, "save_state", lambda *a: None)
    verdict = SimpleNamespace(
        live=SimpleNamespace(track="task", params=params), kind="burn", cid=77
    )
    monkeypatch.setattr(submit, "resolve_competition", lambda *a: verdict)
    monkeypatch.setattr(
        submit,
        "fetch_scene_profiles",
        lambda *a: [] if failure == "route" else [catalog(scene)],
    )
    monkeypatch.setattr(submit, "build_repo_id", lambda *a: "author/scene")
    monkeypatch.setattr(
        submit, "prepare_gated_repository", lambda **k: calls.append("gate")
    )

    def upload(*a, **k):
        calls.append("upload")
        state.update(
            hf_repo_id="author/scene",
            hf_commit="a" * 40,
            hf_url="https://example.invalid",
        )

    monkeypatch.setattr(submit, "perform_upload", upload)
    monkeypatch.setattr(submit, "fetch_package", lambda *a, **k: scene)
    for method in ("contributor_is_bound", "slot_is_free", "evaluator_can_read"):
        monkeypatch.setattr(submit, method, lambda *a: True)

    def confirm(*a):
        calls.append("confirm")

    monkeypatch.setattr(submit, "confirm_payment", confirm)

    def pay(*a, **k):
        calls.append("pay")
        state["burn_tx_hash"] = "fixture-tx"
        return True

    monkeypatch.setattr(submit, "perform_burn", pay)

    def announce(*a):
        calls.append("announce")
        return True

    monkeypatch.setattr(submit, "perform_announce", announce)
    if failure == "changed-local":

        def changed(*a, **k):
            upload()
            (scene / "private/stray.txt").write_text("changed")

        monkeypatch.setattr(submit, "perform_upload", changed)
    expected = 0 if failure in {"none", "paid-resume", "announced"} else 1
    argv = [
        "--log-dir",
        str(tmp_path / "logs"),
        "submit",
        "--scene-package",
        str(scene),
    ]
    if failure == "force-invalid":
        argv.append("--force")
    assert main(argv) == expected
    if failure == "none":
        assert calls == ["gate", "upload", "confirm", "pay", "announce"]
    elif failure == "paid-resume":
        assert calls == ["upload", "announce"]
    elif failure == "announced":
        assert calls == []
    elif failure == "force-invalid":
        assert calls == []
        assert state["burn_tx_hash"] == "fixture-tx"
    else:
        assert "pay" not in calls
        if failure in {"dependency", "account", "route"}:
            assert "upload" not in calls
