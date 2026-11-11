"""CLI materialization and synthetic runtime interfaces, never physics evidence."""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
from pathlib import Path

import pytest
from openroboto_protocol.scene_contract import (
    MAX_SCENE_INSTRUCTION_CHARACTERS,
    profile_catalog,
    profile_ref,
)
from openroboto_protocol.tabletop_assets import canonical_digest

from openroboto import scene_authoring as runtime
from openroboto.cli import main
from openroboto.scene_instruction import instruction_bytes, prepare_execution
from openroboto.scene_package import (
    ScenePackageError,
    execution_input,
)
from tests.test_scene_authoring_bridge import command as authoring_command
from tests.test_scene_authoring_bridge import runtime_case as runtime_case

FIXTURE = Path(__file__).parent / "fixtures/open_scene"


@pytest.fixture
def instruction_case(runtime_case, monkeypatch):
    case = runtime_case
    task = case["task"]
    task["task_definition"]["observation"] = "state"
    task["checker"]["checker"]["definition"] = copy.deepcopy(task["task_definition"])
    task["scene"]["camera"]["image_size"] = profile_catalog()["camera_sizes"]["state"]
    camera = task["scene"]["camera"]
    camera["digest"] = canonical_digest(
        {k: v for k, v in camera.items() if k != "digest"}
    )
    task["execution_contract"]["observation_contract"] = "state"
    task["execution_contract"]["permissions"]["allowed_sensors"] = profile_catalog()[
        "observations"
    ]["state"]
    (case["execution"] / "task.json").write_text(json.dumps(task))
    case["description"]["scene_authoring"]["commands"].append(
        "scene-render-instruction"
    )
    # Synthetic values in the fixed ea43315d wrapper shape, not renderer output.
    configuration = {
        "policy_version": "scene-instruction-admission/2",
        "renderer": "scene-program-text/1",
        "execution_profile": profile_ref(),
        "max_instruction_characters": MAX_SCENE_INSTRUCTION_CHARACTERS,
    }
    policy = {
        "version": configuration["policy_version"],
        "digest": canonical_digest(configuration),
        "renderer_version": configuration["renderer"],
        "renderer_implementation_digest": "c" * 64,
        "installation_manifest_digest": "d" * 64,
        "configuration": configuration,
    }
    description = case["description"]["scene_authoring"]
    description["instruction_admission_policy"] = policy
    description["registry"]["runtime"]["instruction_admission_policy"] = policy
    task["environment"]["digest"] = canonical_digest(description["registry"])
    (case["execution"] / "task.json").write_text(json.dumps(task))
    typed, files = execution_input(case["execution"])
    text = 'Synthetic public text: exact "quotes", α and 0.6800000000000001.'
    finding = {
        "accepted": True,
        "code": "scene_instruction_rendered",
        "schema_valid": True,
        "text": text,
        "character_count": len(text),
        "max_characters": MAX_SCENE_INSTRUCTION_CHARACTERS,
        "expected_instruction_digest": hashlib.sha256(text.encode()).hexdigest(),
        "program_digest": canonical_digest(
            typed.task_definition.model_dump(mode="json")
        ),
        "render_input_digest": "b" * 64,
        "renderer_version": "scene-program-text/1",
        "renderer_implementation_digest": "c" * 64,
        "policy_version": "scene-instruction-admission/2",
        "policy_digest": policy["digest"],
        "actual_instruction_digest": None,
        "delivered_instruction_digest": None,
        "delivery_checked": False,
        "private_marker": "NEVER_PUBLISH",
    }
    case["finding"] = finding
    case["render_body"] = {
        "schema_version": 1,
        "command": "scene-render-instruction",
        "ok": True,
        "capability": "instruction_render",
        "instruction": finding,
        "runtime_version": case["metadata"]["runtime_version"],
        "execution_profile": profile_ref(),
        "installed_environment": task["environment"],
        "input_environment": task["environment"],
        "environment_matches_installation": True,
        "input_task_sha256": hashlib.sha256(files["task.json"]).hexdigest(),
        "package_validated": False,
        "private_reference_created": False,
        "server_admission": "not_run",
        "business_model_calls": 0,
        "private_marker": "NEVER_PUBLISH",
    }
    case["prepared"] = case["output"].parent / "NEW-E"
    case["render_rc"] = 0
    original = runtime._invoke

    def invoke(python, argv, *, timeout):
        if "scene-render-instruction" not in argv:
            return original(python, argv, timeout=timeout)
        case["calls"].append((python, argv, timeout))
        assert "--private" not in argv and "--manifest" not in argv
        assert str(case["output"]) not in argv
        public = Path(argv[argv.index("--output") + 1])
        public.write_text(json.dumps(case["render_body"]))
        if case.get("after_render"):
            case["after_render"]()
        return case["render_rc"], {"private_marker": "NEVER_PUBLISH"}

    monkeypatch.setattr(runtime, "_invoke", invoke)
    return case


def instruction_command(case, *, prepare=True):
    args = [
        "scene",
        "instruction",
        "--execution",
        str(case["execution"]),
        "--output",
        str(case["output"]),
    ]
    if prepare:
        args += ["--prepared-execution", str(case["prepared"])]
    return args


@pytest.mark.parametrize("prepare", [True, False])
def test_formal_instruction_command_uses_same_runtime_and_public_text(
    instruction_case, capsys, prepare
):
    case = instruction_case
    before = {
        str(p.relative_to(case["root"])): p.read_bytes()
        for p in case["root"].rglob("*")
        if p.is_file()
    }
    assert main(instruction_command(case, prepare=prepare)) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["rendering"]["instruction"] == case["finding"]["text"]
    for key in (
        "schema_valid",
        "max_characters",
        "actual_instruction_digest",
        "delivered_instruction_digest",
        "delivery_checked",
    ):
        assert report["rendering"][key] == case["finding"][key]
    assert report["rendering"]["package_validated"] is False
    assert report["local_physics"] == report["server_admission"] == "not_run"
    assert report["reward_status"] == "not_evaluated"
    assert "NEVER_PUBLISH" not in json.dumps(report) + case["output"].read_text()
    assert before == {
        str(p.relative_to(case["root"])): p.read_bytes()
        for p in case["root"].rglob("*")
        if p.is_file()
    }
    if prepare:
        assert report["reference_re_record_required"] is True
        assert (
            json.loads((case["prepared"] / "task.json").read_text())["instruction"]
            == case["finding"]["text"]
        )
        assert not (case["prepared"] / "private").exists()
    else:
        assert not case["prepared"].exists()


def test_missing_optional_capability_is_unsupported_not_generated(
    instruction_case, capsys
):
    case = instruction_case
    case["description"]["scene_authoring"]["commands"].remove(
        "scene-render-instruction"
    )
    assert main(instruction_command(case)) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["capability"] == "unsupported"
    assert report["issues"][0]["code"] == "scene_instruction_unavailable"
    assert "rendering" not in report and not case["prepared"].exists()
    assert not any("scene-render-instruction" in argv for _, argv, _ in case["calls"])


@pytest.mark.parametrize("command", ["record-reference", "self-test"])
def test_new_optional_command_does_not_reject_existing_authoring(
    runtime_case, capsys, command
):
    runtime_case["description"]["scene_authoring"]["commands"].append(
        "scene-render-instruction"
    )
    if command == "self-test":
        runtime_case["private"] = runtime_case["root"] / "private"
        runtime_case["manifest"] = runtime_case["root"] / "submission.json"
    assert main(authoring_command(runtime_case, command)) == 0
    result = json.loads(capsys.readouterr().out)
    assert (
        result["reference_ready" if command == "record-reference" else "accepted"]
        is True
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("program_digest", "0" * 64),
        ("render_input_digest", "invalid"),
        ("expected_instruction_digest", "0" * 64),
        ("renderer_implementation_digest", "wrong"),
        ("policy_version", "wrong"),
        ("policy_digest", "e" * 64),
        ("code", "scene_instruction_consistent"),
        ("schema_valid", False),
        ("max_characters", True),
        ("delivery_checked", True),
        ("actual_instruction_digest", "a" * 64),
        ("delivered_instruction_digest", "b" * 64),
    ],
)
def test_rendered_text_needs_complete_bound_identities(
    instruction_case, capsys, field, value
):
    case = instruction_case
    case["finding"][field] = value
    assert main(instruction_command(case)) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["issues"][0]["code"] == "instruction_result_invalid"
    assert not case["prepared"].exists()


@pytest.mark.parametrize("kind", ["unsupported", "long", "exit", "protocol", "drift"])
def test_renderer_errors_never_create_new_E_or_claim_physics(
    instruction_case, capsys, kind
):
    case = instruction_case
    if kind == "unsupported":
        case["render_body"].update(ok=False, capability="unsupported")
        case["finding"].update(
            accepted=False,
            code="scene_instruction_coverage_unsupported",
            error="NEVER_PUBLISH",
        )
        case["render_rc"] = 1
    elif kind == "long":
        length = MAX_SCENE_INSTRUCTION_CHARACTERS + 1
        case["finding"]["text"] = "x" * length
        case["finding"]["character_count"] = length
        case["finding"]["expected_instruction_digest"] = hashlib.sha256(
            b"x" * length
        ).hexdigest()
    elif kind == "exit":
        case["render_rc"] = 1
    elif kind == "protocol":
        case["metadata"]["protocol_version"] = "0.17.0"
    else:
        case["after_render"] = lambda: (case["execution"] / "stray").write_text(
            "NEVER_PUBLISH"
        )
    assert main(instruction_command(case)) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["local_physics"] == "not_run" and report["ok"] is False
    assert not case["prepared"].exists()
    assert "NEVER_PUBLISH" not in case["output"].read_text()


@pytest.mark.parametrize(
    "kind", ["length", "vision", "coverage", "missing_binding", "semantics", "unknown"]
)
def test_public_unsupported_receipts_retain_full_text_without_truncation(
    instruction_case, capsys, kind
):
    case = instruction_case
    body = case["finding"]
    case["render_body"].update(ok=False, capability="unsupported")
    body["accepted"] = False
    case["render_rc"] = 1
    if kind == "length":
        length = MAX_SCENE_INSTRUCTION_CHARACTERS + 1
        body.update(
            text="α" * length,
            character_count=length,
            code="scene_instruction_length_unsupported",
            expected_instruction_digest=hashlib.sha256(
                ("α" * length).encode()
            ).hexdigest(),
        )
    else:
        body.update(
            text=None,
            character_count=None,
            expected_instruction_digest=None,
            code="NEVER_PUBLISH"
            if kind == "unknown"
            else "scene_instruction_missing_binding"
            if kind == "missing_binding"
            else f"scene_instruction_{kind}_unsupported",
        )
    assert main(instruction_command(case)) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["capability"] == "unsupported"
    assert result["rendering"]["instruction"] == body["text"]
    assert result["local_physics"] == "not_run"
    assert not case["prepared"].exists()
    assert "NEVER_PUBLISH" not in case["output"].read_text()


@pytest.mark.parametrize(
    "target", ["source", "old_private", "manifest", "nested", "existing"]
)
def test_preparation_cannot_mutate_retained_roots(instruction_case, capsys, target):
    case = instruction_case
    paths = {
        "source": case["execution"],
        "old_private": case["root"] / "private/new",
        "manifest": case["root"] / "submission.json",
        "nested": case["execution"] / "new",
        "existing": case["output"].parent / "existing",
    }
    case["prepared"] = paths[target]
    if target == "existing":
        case["prepared"].mkdir()
    assert main(instruction_command(case)) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["local_physics"] == "not_run"
    assert not case["calls"]


def test_preparation_changes_only_instruction_token_and_never_copies_private(tmp_path):
    source = tmp_path / "old/execution"
    shutil.copytree(FIXTURE / "execution", source)
    shutil.copytree(FIXTURE / "private", source.parent / "private")
    shutil.copyfile(FIXTURE / "submission.json", source.parent / "submission.json")
    # The representation must survive even where binary float round-trips do not.
    original = (source / "task.json").read_bytes()
    original = original.replace(b"1.0", b"1.00000000000000000000000000001", 1)
    assert b"1.00000000000000000000000000001" in original
    (source / "task.json").write_bytes(original)
    _, files = execution_input(source)
    before = {
        str(p.relative_to(source.parent)): p.read_bytes()
        for p in source.parent.rglob("*")
        if p.is_file()
    }
    output = tmp_path / "NEW-execution"
    text = 'Exact "quoted" text, Unicode α; line\nnext, 0.6800000000000000001.'
    prepare_execution(source, output, files, text)
    assert b"1.00000000000000000000000000001" in (output / "task.json").read_bytes()
    assert json.loads((output / "task.json").read_bytes())["instruction"] == text
    old = json.loads(original)
    new = json.loads((output / "task.json").read_bytes())
    old.pop("instruction")
    new.pop("instruction")
    assert old == new
    assert before == {
        str(p.relative_to(source.parent)): p.read_bytes()
        for p in source.parent.rglob("*")
        if p.is_file()
    }
    for name, content in files.items():
        if name != "task.json":
            assert (output / name).read_bytes() == content
    assert not (output / "private").exists()
    assert not (output / "submission.json").exists()


@pytest.mark.parametrize(
    "source",
    [
        b'{"x":1.00000000000000000000000001,"instruction":"old","a":{"instruction":"nested"}}\n',
        b'{"instruction":"old \\" quote","array":["instruction",true,null]}',
        b'\xef\xbb\xbf { "instruction" : "old" , "x" : 1e-20 } ',
    ],
)
def test_instruction_token_replacement_preserves_other_bytes(source):
    output = instruction_bytes(source, 'new\n"value"')
    decoded = json.loads(source)
    decoded["instruction"] = 'new\n"value"'
    assert json.loads(output) == decoded
    assert (
        b"1.00000000000000000000000001" not in source
        or b"1.00000000000000000000000001" in output
    )
    assert b"1e-20" not in source or b"1e-20" in output


@pytest.mark.parametrize(
    "source",
    [
        b"{}",
        b"[]",
        b'{"instruction":"a","instruction":"b"}',
        b'{"instruction":1}',
        b'{"instruction":"a","x":NaN}',
        b'{"instruction":"a"} trailing',
        b'{"instruction":"a",}',
        b"\xff",
    ],
)
def test_ambiguous_task_json_is_not_silently_rewritten(source):
    with pytest.raises(ScenePackageError, match="instruction_input_invalid"):
        instruction_bytes(source, "new")


def test_preparation_never_overwrites_or_relaxes_text_limit(tmp_path):
    source = FIXTURE / "execution"
    _, files = execution_input(source)
    output = tmp_path / "NEW"
    with pytest.raises(ScenePackageError, match="instruction_unsupported"):
        prepare_execution(
            source, output, files, "x" * (MAX_SCENE_INSTRUCTION_CHARACTERS + 1)
        )
    assert not output.exists()
    output.mkdir()
    (output / "keep").write_text("keep")
    with pytest.raises(ScenePackageError, match="output_exists"):
        prepare_execution(source, output, files, "new")
    assert (output / "keep").read_text() == "keep"


def test_preparation_retains_utf8_bom_and_escaped_instruction_key(tmp_path):
    source = tmp_path / "source"
    shutil.copytree(FIXTURE / "execution", source)
    raw = (
        (source / "task.json")
        .read_bytes()
        .replace(b'"instruction"', b'"\\u0069nstruction"', 1)
    )
    (source / "task.json").write_bytes(b"\xef\xbb\xbf" + raw)
    _, files = execution_input(source)
    destination = tmp_path / "NEW"
    prepare_execution(source, destination, files, "Exact new text.")
    prepared = (destination / "task.json").read_bytes()
    assert prepared.startswith(b"\xef\xbb\xbf")
    assert b'"\\u0069nstruction"' in prepared
    assert json.loads(prepared)["instruction"] == "Exact new text."


def test_post_probe_drift_cannot_prepare_an_unchecked_source(
    instruction_case, monkeypatch, capsys
):
    case = instruction_case
    invoke = runtime._invoke
    completed = False
    changed = False

    def post_probe(python, argv, *, timeout):
        nonlocal completed, changed
        result = invoke(python, argv, timeout=timeout)
        if "scene-render-instruction" in argv:
            completed = True
        elif completed and not changed and argv[0] == "-c":
            changed = True
            (case["execution"] / "extra").write_text("concurrent change")
        return result

    monkeypatch.setattr(runtime, "_invoke", post_probe)
    assert main(instruction_command(case)) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["issues"][0]["code"] == "execution_drift"
    assert not case["prepared"].exists()


@pytest.mark.parametrize("length", [1001, MAX_SCENE_INSTRUCTION_CHARACTERS])
def test_new_capacity_uses_the_installed_schema_without_truncation(
    instruction_case, capsys, length
):
    case = instruction_case
    text = "α" * length
    case["finding"].update(
        text=text,
        character_count=length,
        expected_instruction_digest=hashlib.sha256(text.encode()).hexdigest(),
    )
    assert main(instruction_command(case)) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["rendering"]["instruction"] == text
    assert (
        json.loads((case["prepared"] / "task.json").read_bytes())["instruction"] == text
    )
    assert report["reference_re_record_required"] is True


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", True),
        ("command", "scene-self-test"),
        ("ok", False),
        ("capability", "local_physics"),
        ("runtime_version", "wrong"),
        ("execution_profile", {}),
        ("installed_environment", {}),
        ("input_environment", {}),
        ("environment_matches_installation", False),
        ("input_task_sha256", "0" * 64),
        ("package_validated", True),
        ("private_reference_created", True),
        ("business_model_calls", True),
        ("server_admission", "passed"),
        ("instruction", "NEVER_PUBLISH"),
    ],
)
def test_fixed_wrapper_needs_exact_public_input_and_runtime_bindings(
    instruction_case, capsys, field, value
):
    case = instruction_case
    case["render_body"][field] = value
    assert main(instruction_command(case)) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["issues"][0]["code"] == "instruction_result_invalid"
    assert not case["prepared"].exists()
    assert "NEVER_PUBLISH" not in case["output"].read_text()


def test_old_synthetic_flat_wrapper_is_not_a_second_protocol(instruction_case, capsys):
    case = instruction_case
    case["render_body"] = copy.deepcopy(case["finding"])
    assert main(instruction_command(case)) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["issues"][0]["code"] == "instruction_result_invalid"
    assert not case["prepared"].exists()


def test_render_policy_must_match_both_describe_and_registry(instruction_case, capsys):
    case = instruction_case
    policy = copy.deepcopy(
        case["description"]["scene_authoring"]["instruction_admission_policy"]
    )
    policy["digest"] = "e" * 64
    case["description"]["scene_authoring"]["instruction_admission_policy"] = policy
    assert main(instruction_command(case)) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["issues"][0]["code"] == "instruction_result_invalid"
    assert not case["prepared"].exists()
