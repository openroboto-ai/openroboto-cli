"""Explicit candidate discovery and v2 definition creation stay offline."""

import argparse
import json

from openroboto_protocol.tabletop_v2 import profile_ref, validate_definition

from openroboto.commands.task_authoring import run_catalog, run_create


def test_create_v2_is_explicit_and_does_not_overwrite(tmp_path):
    target = tmp_path / "definition.json"
    args = argparse.Namespace(
        definition=str(target), profile="panda-tabletop-v2", public_summary=""
    )
    assert run_create(args) == 0
    document = json.loads(target.read_text())
    assert document["execution_profile"] == profile_ref()
    assert not validate_definition(document["task_definition"])
    original = target.read_bytes()
    assert run_create(args) == 1 and target.read_bytes() == original


def test_default_catalog_does_not_advertise_development_as_released(capsys):
    assert run_catalog(argparse.Namespace(json=True, include_development=False)) == 0
    released = json.loads(capsys.readouterr().out)
    assert all(
        p["execution_profile"]["id"] != "panda-tabletop-v2"
        for p in released["profiles"]
    )
    assert run_catalog(argparse.Namespace(json=True, include_development=True)) == 0
    candidate = json.loads(capsys.readouterr().out)["profiles"][-1]
    assert candidate["status"] == "development_candidate"
