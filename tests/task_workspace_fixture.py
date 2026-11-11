"""Shared task workspace configuration for track-routing tests."""

from pathlib import Path

import yaml
from openroboto_protocol.task_contract import execution_profile_ref


def write_task_config(directory: Path, *, profile=None) -> Path:
    path = directory / "miner.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "competition": {
                    "id": 274,
                    "track": "task",
                    "seq": 1,
                    "label": "Task test",
                    "adapter": "task",
                    "params": {
                        "task": {
                            "source": "axis",
                            "execution_profile": profile or execution_profile_ref(),
                        }
                    },
                },
                "backend": {"url": "http://127.0.0.1:8091"},
                "authoring": {
                    "definition": "task.json",
                    "package": "task-package",
                    "solve": "solve.py",
                    "evidence": "evidence",
                },
            }
        )
    )
    return path
