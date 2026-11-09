"""Test-only startup guard for installed S0 runtime commands.

This is instrumentation, not the registered execution gateway or a physics
sandbox. Copy it outside task_source into a fresh test environment; its .pth
bootstrap must fail closed even if Python's site loader catches an exception.
The CLI/protocol/runtime distributions and their identities stay unchanged.
"""

from __future__ import annotations

import atexit
import hashlib
import importlib.abc
import json
import os
import pathlib
import sys
import time
from typing import Any

FORBIDDEN_ROOTS = frozenset(
    {"mujoco", "glfw", "opengl", "torch", "tensorflow", "transformers", "jax"}
)


class S0Violation(RuntimeError):
    """An S0 invocation or effect would exceed pure verification."""


def classify_invocation(argv: list[str], metadata_sha256: str) -> str:
    """Authorize the exact registry probe or describe/pure render, never record."""
    if len(argv) == 3 and argv[:2] == ["-I", "-c"]:
        if hashlib.sha256(argv[2].encode()).hexdigest() == metadata_sha256:
            return "registry"
        raise S0Violation("S0 registry code is not the reviewed probe")
    if len(argv) < 4 or argv[:3] != ["-I", "-m", "task_source.authoring"]:
        raise S0Violation("S0 module invocation is not allowed")
    command, options = argv[3], argv[4:]
    if command == "describe" and options == ["--include-development"]:
        return "describe"
    if command != "scene-render-instruction" or len(options) % 2:
        raise S0Violation("S0 command is not describe or pure render")
    seen = set()
    for key, value in zip(options[::2], options[1::2], strict=True):
        if (
            key not in {"--execution", "--output", "--text-output"}
            or key in seen
            or not value
            or value.startswith("-")
        ):
            raise S0Violation("S0 render options are not allowed")
        seen.add(key)
    if "--execution" not in seen:
        raise S0Violation("S0 render requires explicit E")
    return "pure_render"


def blocked_import(fullname: str) -> bool:
    return fullname.split(".")[0].lower() in FORBIDDEN_ROOTS


def classify_cli_invocation(argv: list[str]) -> str:
    """Parent-side whitelist: do not even launch an installed authoring command."""
    if argv[:3] != ["task", "scene", "instruction"] or len(argv[3:]) % 2:
        raise S0Violation("S0 installed CLI command is not pure instruction")
    seen = set()
    for key, value in zip(argv[3::2], argv[4::2], strict=True):
        if (
            key
            not in {
                "--execution",
                "--output",
                "--prepared-execution",
                "--runtime-python",
                "--timeout-seconds",
            }
            or key in seen
            or not value
        ):
            raise S0Violation("S0 installed CLI options are not allowed")
        seen.add(key)
    if not {"--execution", "--output"} <= seen:
        raise S0Violation("S0 installed CLI requires explicit E and public output")
    return "instruction_and_new_E"


def blocked_audit_event(event: str, args: tuple[Any, ...]) -> bool:
    if event.startswith(("subprocess.", "os.exec")) or event in {
        "os.fork",
        "os.forkpty",
        "os.posix_spawn",
        "os.system",
        "socket.connect",
        "socket.connect_ex",
        "socket.getaddrinfo",
        "socket.sendto",
        "socket.sendmsg",
    }:
        return True
    if event == "ctypes.dlopen" and args:
        name = str(args[0]).lower()
        return any(part in name for part in ("mujoco", "glfw", "libtorch"))
    return False


def _event(config: dict[str, Any], event: str, **fields: Any) -> None:
    payload = (
        json.dumps(
            {
                "event": event,
                "pid": os.getpid(),
                "monotonic": time.monotonic(),
                **fields,
            },
            sort_keys=True,
            allow_nan=False,
        ).encode()
        + b"\n"
    )
    fd = os.open(
        config["audit_path"],
        os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW,
        0o600,
    )
    try:
        os.write(fd, payload)
    finally:
        os.close(fd)


def install() -> None:
    """Install before the reviewed -m/-c body; startup errors terminate Python."""
    config: dict[str, Any] = {}
    try:
        config = json.loads(pathlib.Path(__file__).with_suffix(".json").read_bytes())
        kind = classify_invocation(sys.orig_argv[1:], config["metadata_sha256"])
        if any(blocked_import(name) for name in sys.modules):
            raise S0Violation("native/model module present before S0 instrumentation")

        def reject(event: str) -> None:
            _event(config, "blocked", reason=event)
            raise S0Violation("S0 blocked native, model, network or process effect")

        class ImportGuard(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if blocked_import(fullname):
                    reject("import:" + fullname.split(".")[0])
                return None

        def audit(event: str, args: tuple[Any, ...]) -> None:
            if blocked_audit_event(event, args):
                reject(event)

        def finish() -> None:
            if any(blocked_import(name) for name in sys.modules):
                _event(config, "blocked", reason="forbidden_module_at_exit")
                os._exit(97)
            _event(config, "finish", classification=kind)

        sys.meta_path.insert(0, ImportGuard())
        sys.addaudithook(audit)
        atexit.register(finish)
        _event(config, "start", classification=kind)
    except BaseException:
        # Raising from .pth alone is insufficient: site.py catches that error.
        try:
            if config:
                _event(config, "startup_rejected")
        finally:
            os._exit(97)
