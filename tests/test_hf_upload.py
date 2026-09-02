"""What the upload step of `openroboto submit` writes into the miner's own
repository.

Everything here ends up on Hugging Face under the miner's account, so a wrong
value is not an internal inconsistency -- it is a claim published in their name.
"""

from __future__ import annotations

import json

from openroboto.huggingface.upload import _write_round_info, push_model


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
    push_model(str(tmp_path), "someone/private-model", "hf_x", round_num=1)

    assert "visibility" not in calls, (
        "upload flipped the repository's visibility; a private repo must stay private"
    )


def test_round_info_names_the_season_base_model_not_pi05(tmp_path) -> None:
    """`round_info.json` ships inside the miner's repo, so its `model` key is a
    claim about the artifact it sits next to. It used to be the literal `pi05`
    regardless of what was trained."""
    _write_round_info(tmp_path, 1, None, "lingbot_vla")
    info = json.loads((tmp_path / "round_info.json").read_text(encoding="utf-8"))
    assert info["model"] == "lingbot_vla"
    assert "pi05" not in json.dumps(info)


def test_round_info_says_nothing_rather_than_guessing(tmp_path) -> None:
    """A season that has not named a base model leaves the key empty. Guessing
    here is how a LingBot checkpoint ended up labelled pi0.5."""
    _write_round_info(tmp_path, 1, None, "")
    info = json.loads((tmp_path / "round_info.json").read_text(encoding="utf-8"))
    assert info["model"] == ""
