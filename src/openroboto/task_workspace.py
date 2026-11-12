"""Track-specific workspace data; contracts come from the protocol package."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from openroboto_protocol.scene_contract import task_profile_catalog
from openroboto_protocol.task_contract import TaskContractError
from openroboto_protocol.task_profiles import validate_task_document

from openroboto.config import ConfigError, Settings


def task_settings(config: str) -> Settings:
    settings = Settings.load(config)
    if settings.competition.get("track") != "task":
        raise ConfigError(
            "This command needs a task workspace. Run openroboto init "
            "and select a task season."
        )
    return settings


def catalog_for(profile: object) -> dict[str, Any]:
    for item in task_profile_catalog(include_development=True):
        if item["execution_profile"] == profile:
            item["authoring_commands"] = {
                "create": "openroboto create task.json",
                "build": "openroboto build",
                "record_reference": "openroboto record-reference --script solve.py",
                "self_test": "openroboto check",
            }
            return {"profiles": [item]}
    raise ConfigError(
        "The season selects an unknown execution profile. Update "
        "the CLI or ask the operator to correct it."
    )


def definition_for(profile: object) -> dict[str, Any]:
    catalog = catalog_for(profile)["profiles"][0]
    if catalog["id"] == "panda-tabletop-v2":
        from openroboto_protocol.tabletop_v2 import example_definition

        definition = example_definition()
    elif catalog["id"] == "panda-tabletop-v1":
        position = catalog["limits"]["position_m"]
        # The bundled example uses this released one-cube task.
        start = [position["x"][0] + 0.02, position["y"][0] + 0.06, position["z"][0]]
        target = [position["x"][1] - 0.02, start[1], start[2]]
        definition = {
            "template": "place_in_zones",
            "instance_count": 2,
            "seed": 0,
            "objects": [
                {
                    "id": "cube_a",
                    "asset_id": catalog["assets"][0]["id"],
                    "position": start,
                }
            ],
            "goals": [{"object_id": "cube_a", "target_position": target}],
        }
    elif catalog["id"] == "panda-open-scene-v1":
        from openroboto.scene_workflow import starter_files

        return dict(json.loads(starter_files()["task.json"]))
    else:
        raise ConfigError(
            "Imported scenes use openroboto scene build with explicit "
            "execution/private roots; this profile has no JSON "
            "authoring template."
        )
    document = {
        "schema_version": 1,
        "execution_profile": profile,
        "task_definition": definition,
    }
    issues = validate_task_document(document, require_profile=True)
    if issues:
        raise TaskContractError(issues)
    return document


def workspace_path(settings: Settings, config: str, key: str) -> Path:
    defaults = {
        "definition": "task.json",
        "package": "task-package",
        "solve": "solve.py",
        "evidence": "evidence",
    }
    value = Path(settings.authoring.get(key, defaults[key])).expanduser()
    return (Path(config).resolve().parent / value).resolve()


def json_text(value: object) -> str:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
        + "\n"
    )
