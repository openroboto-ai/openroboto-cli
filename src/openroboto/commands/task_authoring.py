"""Restricted task creation and an explicit bridge to an installed simulator.

The lightweight CLI creates and validates authoring JSON without a simulator.
Build/reference/self-test run in the author's chosen runtime interpreter; no
author script is executed by this module or sent to a remote admission worker.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys
from collections.abc import Mapping
from typing import Any

from openroboto_protocol.task_contract import TaskContractError, execution_profile_ref
from openroboto_protocol.task_profiles import (
    parse_public_summary,
    task_profile_catalog,
    validate_task_document,
)

from openroboto.console import fail, hint, say


def add_authoring_parsers(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    """Add commands without changing any existing task command or argument."""
    catalog = subparsers.add_parser(
        "catalog", help="show supported robots, objects and authoring ranges"
    )
    catalog.add_argument("--json", action="store_true")
    catalog.add_argument(
        "--include-development",
        action="store_true",
        help="include explicitly unverified candidate profiles",
    )
    catalog.set_defaults(handler=run_catalog)

    create = subparsers.add_parser(
        "create", help="write a new restricted task definition to edit"
    )
    create.add_argument(
        "definition", help="new JSON file; existing files are never overwritten"
    )
    create.add_argument(
        "--profile",
        choices=["panda-tabletop-v1", "panda-tabletop-v2"],
        default="panda-tabletop-v1",
    )
    create.add_argument(
        "--public-summary",
        default="",
        help="explicit public synopsis JSON; omitted means private",
    )
    create.set_defaults(handler=run_create)

    build = subparsers.add_parser(
        "build", help="build a task package in a separately installed runtime"
    )
    build.add_argument("--definition", required=True)
    build.add_argument("--output", required=True)
    build.add_argument("--task-id", default="")
    build.add_argument("--title", default="")
    build.add_argument("--public-summary", default="")
    _runtime_option(build)
    build.set_defaults(handler=run_build)

    reference = subparsers.add_parser(
        "record-reference", help="record local reference actions using the runtime"
    )
    reference.add_argument("package")
    reference_source = reference.add_mutually_exclusive_group(required=True)
    reference_source.add_argument(
        "--script",
        help="local solve(runtime, task, instance) script outside the package",
    )
    reference_source.add_argument(
        "--example",
        action="store_true",
        help="local one-cube example; not a general solver or admission proof",
    )
    _runtime_option(reference)
    reference.set_defaults(handler=run_reference)

    check = subparsers.add_parser(
        "self-test", help="run physical reference admission using the installed runtime"
    )
    check.add_argument("package")
    check.add_argument(
        "--output", default="", help="optional report file, outside the package"
    )
    _runtime_option(check)
    check.set_defaults(handler=run_self_test)


def _runtime_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--runtime-python",
        default=sys.executable,
        help=(
            "Python with the verified worker and MuJoCo installed; "
            "defaults to this interpreter"
        ),
    )


def run_catalog(args: argparse.Namespace) -> int:
    """Expose normative options without contacting the backend or a model."""
    profiles = task_profile_catalog(
        include_development=getattr(args, "include_development", False)
    )
    if args.json:
        say(json.dumps({"profiles": profiles}, ensure_ascii=False, sort_keys=True))
    else:
        for profile in profiles:
            ref = profile["execution_profile"]
            say(f"{profile['title']} ({ref['id']} @ {ref['version']})")
            say(profile["description"])
            say(f"Robot: {profile['robot']}")
            say(
                "Allowed objects: "
                + ", ".join(asset["id"] for asset in profile["assets"])
            )
            say(f"Limits: {json.dumps(profile['limits'], sort_keys=True)}")
            say(f"Profile digest: {ref['digest']}")
        hint(
            "These are allowed options, not proof "
            "that a new task is physically solvable."
        )
    return 0


def _read_json(path: str) -> Any:
    return json.loads(pathlib.Path(path).expanduser().read_text(encoding="utf-8"))


def _definition(path: str) -> dict[str, Any]:
    value = _read_json(path)
    if (
        not isinstance(value, Mapping)
        or type(value.get("schema_version")) is not int
        or value.get("schema_version") != 1
    ):
        raise ValueError("The authoring document must declare schema_version=1.")
    allowed = {
        "schema_version",
        "execution_profile",
        "task_definition",
        "public_summary",
    }
    if set(value) - allowed:
        raise ValueError(
            f"Unsupported authoring fields: {sorted(set(value) - allowed)}"
        )
    issues = validate_task_document(value, require_profile=True)
    if issues:
        raise TaskContractError(issues)
    return dict(value)


def run_create(args: argparse.Namespace) -> int:
    """Create only a new editable definition; no upload, payment or execution."""
    profile = task_profile_catalog()[0]
    ranges = profile["limits"]["position_m"]
    document: dict[str, Any] = {
        "schema_version": 1,
        "execution_profile": execution_profile_ref(),
        "task_definition": {
            "template": "place_in_zones",
            "instance_count": 2,
            "seed": 0,
            "objects": [
                {
                    "id": "cube_a",
                    "asset_id": profile["assets"][0]["id"],
                    "position": [
                        round(ranges["x"][0] + 0.02, 3),
                        round(ranges["y"][0] + 0.06, 3),
                        ranges["z"][0],
                    ],
                }
            ],
            "goals": [
                {
                    "object_id": "cube_a",
                    "target_position": [
                        round(ranges["x"][1] - 0.02, 3),
                        round(ranges["y"][0] + 0.06, 3),
                        ranges["z"][0],
                    ],
                }
            ],
        },
    }
    try:
        if getattr(args, "profile", "panda-tabletop-v1") == "panda-tabletop-v2":
            from openroboto_protocol.tabletop_v2 import example_definition, profile_ref

            document = {
                "schema_version": 1,
                "execution_profile": profile_ref(),
                "task_definition": example_definition(),
            }
        if args.public_summary:
            document["public_summary"] = parse_public_summary(
                _read_json(args.public_summary),
                expected_profile=document["execution_profile"],
            )
        issues = validate_task_document(document, require_profile=True)
        if issues:
            raise TaskContractError(issues)
        target = pathlib.Path(args.definition).expanduser()
        with target.open("x", encoding="utf-8") as stream:
            json.dump(
                document,
                stream,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
                allow_nan=False,
            )
            stream.write("\n")
    except (OSError, ValueError) as exc:
        fail(str(exc))
        return 1
    say(f"Created {target}. Edit only the published object, position and goal options.")
    hint(
        "This is a definition, not an admitted task. "
        "Build it, record a local reference and run self-test."
    )
    return 0


def _runtime(args: argparse.Namespace, arguments: list[str]) -> int:
    try:
        result = subprocess.run(
            [
                str(pathlib.Path(args.runtime_python).expanduser()),
                "-m",
                "task_source.authoring",
                *arguments,
            ],
            check=False,
        )
    except OSError as exc:
        fail(f"Cannot start the task runtime: {exc}")
        hint(
            "Install the verified worker runtime separately and pass "
            "--runtime-python /path/to/its/bin/python. "
            "The CLI does not install a simulator."
        )
        return 1
    if result.returncode:
        fail(
            "The task runtime did not complete successfully. "
            "No physical success is claimed."
        )
        hint(
            "It must contain the verified task_source.authoring module "
            "and its simulator dependencies; use --runtime-python "
            "to select that environment."
        )
        return 1
    return 0


def run_build(args: argparse.Namespace) -> int:
    """Reject unsupported definitions before invoking physical package construction."""
    try:
        document = _definition(args.definition)
        if pathlib.Path(args.output).expanduser().exists():
            raise ValueError(
                "Choose a new output directory; task packages are not overwritten."
            )
        if args.public_summary:
            parse_public_summary(
                _read_json(args.public_summary),
                expected_profile=document["execution_profile"],
            )
    except (OSError, ValueError) as exc:
        fail(str(exc))
        return 1
    command = [
        "build",
        "--definition",
        str(pathlib.Path(args.definition).expanduser().resolve()),
        "--output",
        str(pathlib.Path(args.output).expanduser().resolve()),
    ]
    for flag, value in (
        ("--task-id", args.task_id),
        ("--title", args.title),
        ("--public-summary", args.public_summary),
    ):
        if value:
            if flag == "--public-summary":
                value = str(pathlib.Path(value).expanduser().resolve())
            command.extend([flag, value])
    return _runtime(args, command)


def _external_file(
    path: str, package: pathlib.Path, *, must_exist: bool
) -> pathlib.Path:
    target = pathlib.Path(path).expanduser().resolve()
    if target.is_relative_to(package):
        raise ValueError(
            "Keep scripts and self-test reports outside the task package; "
            "they must not change its identity."
        )
    if must_exist and not target.is_file():
        raise ValueError(f"Local file does not exist: {target}")
    return target


def run_reference(args: argparse.Namespace) -> int:
    """Explicit local script execution is delegated to the selected author runtime."""
    try:
        package = pathlib.Path(args.package).expanduser().resolve()
        if not package.is_dir():
            raise ValueError("Select an existing task package directory.")
        command = ["record-reference", str(package)]
        if args.example:
            command.append("--example")
        else:
            script = _external_file(args.script, package, must_exist=True)
            command.extend(["--script", str(script)])
    except (OSError, ValueError) as exc:
        fail(str(exc))
        return 1
    hint(
        "The selected runtime executes this local script on your machine. "
        "Do not upload the script as task content."
    )
    return _runtime(args, command)


def run_self_test(args: argparse.Namespace) -> int:
    """Run physical checks without confusing them with the lightweight format check."""
    try:
        package = pathlib.Path(args.package).expanduser().resolve()
        if not package.is_dir():
            raise ValueError("Select an existing task package directory.")
        command = ["self-test", str(package)]
        if args.output:
            output = _external_file(args.output, package, must_exist=False)
            if output.exists():
                raise ValueError(
                    "Choose a new report file; existing evidence is not overwritten."
                )
            command.extend(["--output", str(output)])
    except (OSError, ValueError) as exc:
        fail(str(exc))
        return 1
    return _runtime(args, command)
