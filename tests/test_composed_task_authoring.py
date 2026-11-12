"""Explicit candidate discovery and v2 definition creation stay offline."""

import argparse
import json

from openroboto_protocol.tabletop_v2 import profile_ref, validate_definition

from openroboto.commands.task_authoring import run_catalog, run_create


def test_create_v2_is_explicit_and_does_not_overwrite(tmp_path):
    from tests.task_workspace_fixture import write_task_config

    config = write_task_config(tmp_path, profile=profile_ref())
    target = tmp_path / "definition.json"
    args = argparse.Namespace(
        config=str(config),
        definition=str(target),
        profile="panda-tabletop-v2",
        public_summary="",
        from_execution="",
    )
    assert run_create(args) == 0
    document = json.loads(target.read_text())
    assert document["execution_profile"] == profile_ref()
    assert not validate_definition(document["task_definition"])
    original = target.read_bytes()
    assert run_create(args) == 1 and target.read_bytes() == original


def test_catalog_respects_exact_season_profile(tmp_path, capsys):
    from tests.task_workspace_fixture import write_task_config

    config = write_task_config(tmp_path, profile=profile_ref())
    assert run_catalog(argparse.Namespace(config=str(config), json=True)) == 0
    profiles = json.loads(capsys.readouterr().out)["profiles"]
    assert len(profiles) == 1
    assert profiles[0]["execution_profile"] == profile_ref()
    assert profiles[0]["status"] == "development_candidate"
