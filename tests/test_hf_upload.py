"""What the upload step of `openroboto submit` writes into the miner's own
repository.

Everything here ends up on Hugging Face under the miner's account, so a wrong
value is not an internal inconsistency -- it is a claim published in their name.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from openroboto.huggingface.upload import _write_run_info, push_model


def test_an_existing_private_repo_is_not_published(monkeypatch, tmp_path) -> None:
    """🔴 Uploading must not flip a repository's visibility.

    `docs/specs/10` §2.5 says a miner's repo may stay private indefinitely --
    it only has to add the official read-only account. Publishing is also not
    undoable: whoever fetched the weights while the repo was open still has
    them. This used to call `update_repo_visibility(..., "public")` on every
    upload.
    """
    import huggingface_hub

    calls: list[str] = []

    class _Api:
        def __init__(self, token: str = "") -> None:
            pass

        def repo_info(self, repo_id: str, repo_type: str = "model") -> object:
            return type("R", (), {"sha": "a" * 40})()

        def update_repo_visibility(self, *a: object, **k: object) -> None:
            calls.append("visibility")

    monkeypatch.setattr(huggingface_hub, "HfApi", _Api)
    monkeypatch.setattr(
        huggingface_hub, "create_repo", lambda **k: calls.append("create")
    )
    monkeypatch.setattr(
        huggingface_hub,
        "upload_folder",
        lambda **k: f"https://huggingface.co/x/y/commit/{'a' * 40}",
    )

    (tmp_path / "model.safetensors").write_bytes(b"w")
    push_model(str(tmp_path), "someone/private-model", "hf_x", competition_id=1)

    assert "visibility" not in calls, (
        "upload flipped the repository's visibility; a private repo must stay private"
    )


def test_run_info_names_the_season_base_model_not_pi05(tmp_path) -> None:
    """`run_info.json` ships inside the miner's repo, so its `model` key is a
    claim about the artifact it sits next to. It used to be the literal `pi05`
    regardless of what was trained."""
    _write_run_info(tmp_path, 1, None, "lingbot_vla")
    info = json.loads((tmp_path / "run_info.json").read_text(encoding="utf-8"))
    assert info["model"] == "lingbot_vla"
    assert "pi05" not in json.dumps(info)


def test_run_info_says_nothing_rather_than_guessing(tmp_path) -> None:
    """A season that has not named a base model leaves the key empty. Guessing
    here is how a LingBot checkpoint ended up labelled pi0.5."""
    _write_run_info(tmp_path, 1, None, "")
    info = json.loads((tmp_path / "run_info.json").read_text(encoding="utf-8"))
    assert info["model"] == ""


def test_a_task_package_is_uploaded_without_run_info(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The validator's package digest counts **every** file.

    `push_model` normally writes `run_info.json` into the directory it uploads.
    For a task package that file would be part of the repository, so the digest
    the miner checked locally would stop matching the one the subnet stores.
    """
    import huggingface_hub

    class _Api:
        def __init__(self, token: str = "") -> None:
            pass

        def repo_info(self, repo_id: str, repo_type: str = "model") -> object:
            return type("R", (), {"sha": "a" * 40})()

    monkeypatch.setattr(huggingface_hub, "HfApi", _Api)
    monkeypatch.setattr(huggingface_hub, "create_repo", lambda **k: None)
    monkeypatch.setattr(
        huggingface_hub,
        "upload_folder",
        lambda **k: f"https://huggingface.co/x/y/commit/{'a' * 40}",
    )

    (tmp_path / "task.json").write_text("{}", encoding="utf-8")
    push_model(
        str(tmp_path),
        "someone/task-package",
        "hf_x",
        competition_id=1,
        write_run_info=False,
        artifact="task package",
    )

    assert not (tmp_path / "run_info.json").exists()


def test_the_default_repository_name_is_refused_for_a_task_season() -> None:
    """`pi05-<hotkey>` is a claim about a model; a task package has no honest
    default name, so the miner must name the repository."""
    from openroboto.config import ConfigError, Settings
    from openroboto.huggingface.repository import build_repo_id

    settings = Settings.from_mapping(
        {
            "competition": {"track": "task", "seq": 1},
            "huggingface": {"username": "someone"},
            "subnet": {"hotkey_ss58": "5" + "M" * 47},
        }
    )
    with pytest.raises(ConfigError, match=r"huggingface\.repo_id"):
        build_repo_id(settings)
