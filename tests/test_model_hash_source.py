"""Where `model_hash` comes from.

The fingerprint is the sha256 of the repository's **LFS object hashes**, and
those are computed by HuggingFace while it receives the push. There is no such
value in a local checkpoint directory, so the only possible source is the HF
tree API, asked **after** the upload and **before** the fee is paid.

The backend recomputes the same fingerprint at evaluation time. If the two sides
disagree the miner is the one who pays, so several of these cases are about the
request being shaped exactly like the backend's, not about it being reasonable.

Every case here is offline: `urlopen` is replaced, nothing leaves the machine.
"""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
from typing import Any

import pytest
from openroboto_protocol.model_hash import fingerprint_lfs_sha256

from openroboto.huggingface import tree

WEIGHT_SHAS = ["a" * 64, "b" * 64, "c" * 64]

TREE = [
    {"type": "file", "path": "config.json", "size": 900},
    {"type": "file", "path": "model-00001.safetensors", "lfs": {"oid": WEIGHT_SHAS[0]}},
    {"type": "file", "path": "model-00002.safetensors", "lfs": {"oid": WEIGHT_SHAS[1]}},
    {"type": "directory", "path": "assets"},
    {"type": "file", "path": "assets/norm_stats.json", "size": 300},
    {"type": "file", "path": "assets/weights.bin", "lfs": {"oid": WEIGHT_SHAS[2]}},
]


class _Response(io.BytesIO):
    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _serve(
    monkeypatch: pytest.MonkeyPatch, body: Any, seen: list[urllib.request.Request]
) -> None:
    def _urlopen(request: urllib.request.Request, timeout: float) -> _Response:
        seen.append(request)
        return _Response(json.dumps(body).encode())

    monkeypatch.setattr(tree, "urlopen", _urlopen)


def _explode(monkeypatch: pytest.MonkeyPatch, exc: Exception) -> None:
    def _urlopen(request: urllib.request.Request, timeout: float) -> _Response:
        raise exc

    monkeypatch.setattr(tree, "urlopen", _urlopen)


def test_only_lfs_files_take_part_in_the_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`config.json` and `norm_stats.json` travel as ordinary git blobs, so
    editing one of them must not change the fingerprint -- otherwise a
    plagiarist escapes by touching the metadata."""
    _serve(monkeypatch, TREE, [])

    assert tree.fetch_model_hash("miner/pi05-x", "a" * 40) == fingerprint_lfs_sha256(
        WEIGHT_SHAS
    )


def test_the_fingerprint_does_not_depend_on_the_order_of_the_listing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HF does not promise an order, and re-uploading the same weights must not
    produce a different fingerprint."""
    _serve(monkeypatch, list(reversed(TREE)), [])
    reversed_hash = tree.fetch_model_hash("miner/pi05-x", "a" * 40)

    _serve(monkeypatch, TREE, [])
    assert reversed_hash == tree.fetch_model_hash("miner/pi05-x", "a" * 40)


def test_a_repo_with_no_lfs_object_yields_the_empty_sentinel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty is a sentinel, not a fingerprint: it means the weights never
    arrived. This function reports it; refusing to pay for it is the caller's
    job (see `test_commands.py`)."""
    _serve(monkeypatch, [entry for entry in TREE if "lfs" not in entry], [])

    assert tree.fetch_model_hash("miner/pi05-x", "a" * 40) == ""


def test_the_request_is_shaped_like_the_backend_s(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """🔴 One GET of `/tree/<commit>?recursive=true`, and nothing else.

    Copied from `app/services/ingest/verification/hf.py` on the backend side.
    The two sides have to see the same file list or the fingerprints differ, and
    the only person who finds out is the miner whose submission is refused.
    """
    seen: list[urllib.request.Request] = []
    _serve(monkeypatch, TREE, seen)

    tree.fetch_model_hash("miner/pi05-x", "a" * 40)

    assert len(seen) == 1
    assert seen[0].full_url == (
        "https://huggingface.co/api/models/miner/pi05-x/tree/"
        + "a" * 40
        + "?recursive=true"
    )


def test_the_token_is_sent_when_there_is_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real track allows private repositories, and without the token those
    answer 404 -- indistinguishable from a typo in the repo name."""
    seen: list[urllib.request.Request] = []
    _serve(monkeypatch, TREE, seen)

    tree.fetch_model_hash("miner/pi05-x", "a" * 40, "hf_secret")
    assert seen[0].get_header("Authorization") == "Bearer hf_secret"

    seen.clear()
    tree.fetch_model_hash("miner/pi05-x", "a" * 40)
    assert seen[0].get_header("Authorization") is None


@pytest.mark.parametrize(
    ("code", "expected"), [(404, "token"), (401, "token"), (503, "their side")]
)
def test_an_http_failure_says_what_to_do_next(
    monkeypatch: pytest.MonkeyPatch, code: int, expected: str
) -> None:
    """A private repository answers 404 for a token that cannot see it, so 404
    and 401 both have to mention the token; 5xx is ours to wait out, not the
    miner's to fix."""
    _explode(
        monkeypatch,
        urllib.error.HTTPError("http://x", code, "boom", {}, None),  # type: ignore[arg-type]
    )

    with pytest.raises(tree.TreeError) as caught:
        tree.fetch_model_hash("miner/pi05-x", "a" * 40)
    assert expected in str(caught.value)


def test_a_network_failure_is_not_reported_as_a_bad_upload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HF jitter is infrastructure, not the miner's model (AGENTS.md §4)."""
    _explode(monkeypatch, urllib.error.URLError("connection reset"))

    with pytest.raises(tree.TreeError) as caught:
        tree.fetch_model_hash("miner/pi05-x", "a" * 40)
    assert "nothing has been paid" in str(caught.value)


def test_a_listing_that_is_not_a_list_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HF answering with an error object rather than a tree must not turn into
    "this repo has no weights" -- those two have opposite next steps."""
    _serve(monkeypatch, {"error": "Repository not found"}, [])

    with pytest.raises(tree.TreeError):
        tree.fetch_model_hash("miner/pi05-x", "a" * 40)
