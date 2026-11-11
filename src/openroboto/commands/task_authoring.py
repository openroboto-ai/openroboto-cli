"""Task authoring verbs using the official bundled Docker runtime."""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import shutil
import tempfile
from collections.abc import Mapping
from typing import Any

from openroboto_protocol.task_contract import TaskContractError
from openroboto_protocol.task_profiles import (
    parse_public_summary,
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
    catalog.add_argument("--config", default="miner.yaml")
    catalog.add_argument(
        "--include-development",
        action="store_true",
        help="include explicitly unverified candidate profiles",
    )
    catalog.set_defaults(handler=run_catalog)

    create = subparsers.add_parser(
        "create", help="write a new restricted task definition to edit"
    )
    create.add_argument("--config", default="miner.yaml")
    create.add_argument(
        "definition",
        nargs="?",
        default="task.json",
        help="new JSON file; existing files are never overwritten",
    )
    create.add_argument(
        "--profile",
        choices=["panda-tabletop-v1", "panda-tabletop-v2"],
        default="",
    )
    create.add_argument(
        "--public-summary",
        default="",
        help="explicit public synopsis JSON; omitted means private",
    )
    create.set_defaults(handler=run_create)

    reference = subparsers.add_parser(
        "record-reference", help="record local reference actions using the runtime"
    )
    reference.add_argument("package", nargs="?", default="")
    reference.add_argument("--config", default="miner.yaml")
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
    reference.set_defaults(handler=run_reference)

    check = subparsers.add_parser(
        "self-test",
        help="run physical reference admission using worker admission in"
        " the official container",
    )
    check.add_argument("package", nargs="?", default="")
    check.add_argument("--config", default="miner.yaml")
    check.add_argument(
        "--output", default="", help="optional report file, outside the package"
    )
    check.set_defaults(handler=run_self_test, expect_digest="", repo="")


def run_catalog(args: argparse.Namespace) -> int:
    """Expose normative options without contacting the backend or a model."""
    from openroboto.task_workspace import catalog_for, task_settings

    settings = task_settings(args.config)
    profiles = catalog_for(
        settings.competition_params.get("task", {}).get("execution_profile")
    )["profiles"]
    if args.json:
        say(json.dumps({"profiles": profiles}, ensure_ascii=False, sort_keys=True))
    else:
        for profile in profiles:
            ref = profile["execution_profile"]
            if ref["id"] == "panda-open-scene-v1":
                say(f"Imported scenes ({ref['id']} @ {ref['version']})")
                say("Development contract only; server support is not established.")
                say(
                    f"Robot: {profile['robot']}; "
                    f"action dimensions: {profile['action_dimensions']}"
                )
                say(f"Profile digest: {ref['digest']}")
                continue
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
    if isinstance(value, Mapping) and value.get("schema_version") == 3:
        raise ValueError(
            "Use `openroboto scene build --execution <directory> "
            "--private <directory> --output <new-directory>` for schema-3 scenes."
        )
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
    from openroboto.task_workspace import definition_for, task_settings

    settings = task_settings(args.config)
    selected = settings.competition_params.get("task", {}).get("execution_profile")
    if args.profile and args.profile != selected["id"]:
        fail("--profile differs from this workspace's profile. Use catalog.json.")
        return 1
    document = definition_for(selected)
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
    from openroboto.task_runtime import TaskRuntime
    from openroboto.task_workspace import task_settings

    settings = task_settings(args.config)
    if (
        document["execution_profile"]
        != settings.competition_params["task"]["execution_profile"]
    ):
        fail("Definition execution_profile differs from the season. Use catalog.json.")
        return 1
    output = pathlib.Path(args.output).expanduser().resolve()
    runtime = TaskRuntime(settings)
    if getattr(args, "dry_run", False):
        say(f"docker build -t {runtime.tag} {runtime.context}; build package {output}")
        return 0
    runtime.prepare()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="openroboto-build-", dir=output.parent
    ) as temporary:
        work = pathlib.Path(temporary)
        definition = pathlib.Path(args.definition).resolve()
        command = [
            "build",
            "--definition",
            str(definition),
            "--output",
            str(work / "package"),
        ]
        for flag, value in (("--task-id", args.task_id), ("--title", args.title)):
            if value:
                command.extend([flag, value])
        mounts = [(definition, False), (work, True)]
        if args.public_summary:
            public = pathlib.Path(args.public_summary).expanduser().resolve()
            command.extend(["--public-summary", str(public)])
            mounts.append((public, False))
        result = runtime.run(command, mounts)
        if result.get("accepted") is False:
            fail(str(result["error"]))
            return 1
        (work / "package").rename(output)
    from openroboto.task_package import package_digest
    from openroboto.task_workspace import json_text, workspace_path

    evidence = workspace_path(settings, args.config, "evidence")
    if evidence.is_relative_to(output):
        raise ValueError("Keep evidence outside the task package.")
    evidence.mkdir(parents=True, exist_ok=True)
    (evidence / "build.json").write_text(
        json_text(
            {
                "definition_sha256": hashlib.sha256(
                    definition.read_bytes()
                ).hexdigest(),
                "package_digest": package_digest(output),
            }
        )
    )
    say(
        f"Built {output}. Run openroboto check to record "
        "and verify a reference solution."
    )
    return 0


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
    from openroboto.task_workspace import task_settings, workspace_path

    settings = task_settings(args.config)
    args.package = args.package or str(workspace_path(settings, args.config, "package"))
    try:
        package = pathlib.Path(args.package).expanduser().resolve()
        if not package.is_dir():
            raise ValueError("Select an existing task package directory.")
        _legacy_package_only(package, "record-reference")
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
    from openroboto.task_runtime import TaskRuntime

    runtime = TaskRuntime(settings)
    runtime.prepare()
    with tempfile.TemporaryDirectory(prefix="openroboto-reference-") as temporary:
        work = pathlib.Path(temporary) / "package"
        shutil.copytree(package, work)
        command[1] = str(work)
        mounts = [(work, True)]
        if not args.example:
            mounts.append((script, False))
        result = runtime.run(command, mounts, timeout=900)
        if result.get("accepted") is False:
            fail(str(result["error"]))
            return 1
        shutil.copyfile(work / "reference.json", package / "reference.json")
    say(json.dumps(result, sort_keys=True))
    return 0


def run_self_test(args: argparse.Namespace) -> int:
    """Run physical checks without confusing them with the lightweight format check."""
    from openroboto.task_workspace import task_settings, workspace_path

    settings = task_settings(args.config)
    args.package = args.package or str(workspace_path(settings, args.config, "package"))
    try:
        package = pathlib.Path(args.package).expanduser().resolve()
        if not package.is_dir():
            raise ValueError("Select an existing task package directory.")
        _legacy_package_only(package, "self-test")
    except (OSError, ValueError) as exc:
        fail(str(exc))
        return 1
    from openroboto.commands.task_check import run as check_task

    args.path = args.package
    args.static_only = False
    args.example = False
    args.script = ""
    args.json = True
    return check_task(args, settings)


def _legacy_package_only(package: pathlib.Path, command: str) -> None:
    """Never send a scene execution root to a recorder that writes v1 references."""
    scene = (package / "execution/task.json").is_file()
    if (package / "task.json").is_file():
        document = _read_json(str(package / "task.json"))
        scene = scene or (
            isinstance(document, Mapping) and document.get("schema_version") == 3
        )
    if scene:
        raise ValueError(
            f"Use `openroboto scene {command} <dual-root-package> "
            "--output <external-report>` for schema-3 references. "
            "The legacy recorder must not write references into execution/."
        )
