"""The file listing of an uploaded repository, and the model fingerprint made
from it.

## Why this cannot be computed from the local checkpoint

The fingerprint is the sha256 of the repository's **LFS object hashes**, and an
LFS object hash does not exist until HuggingFace has taken the file: it is
computed on their side during the push. There is no local directory to walk.
So the order is fixed and cannot be reordered:

    upload  →  ask HF for the tree of that commit  →  fingerprint  →  pay  →  announce

## Why it is fetched exactly the way the backend fetches it

The backend recomputes this fingerprint at evaluation time and compares. If the
two sides disagree the miner is the one who pays for it -- a mismatch reads as
"these are not the weights you pinned on chain". So this is a deliberate copy
of the **request shape** in the backend's
`app/services/ingest/verification/hf.py`: one GET of
`/api/models/{repo}/tree/{revision}?recursive=true`, and whatever that single
response contains is the whole input.

⚠️ In particular, **the `Link: rel="next"` header is not followed**, because
the backend does not follow it either. Paginating here would be more correct in
the abstract and wrong in practice: on a repository large enough to paginate,
the two sides would compute different fingerprints and only the miner would
find out. The day that matters, both sides change together.

The algorithm itself is **not** implemented here -- it comes from
`openroboto_protocol.model_hash` (red line #1). Only the I/O the protocol
package deliberately left to its callers lives in this module.
"""

from __future__ import annotations

import json
import logging
import urllib.error
from typing import Any

from openroboto_protocol.model_hash import model_hash_from_hf_tree

from openroboto.http_client import build_request, urlopen

logger = logging.getLogger(__name__)

HF_API = "https://huggingface.co"

#: Longer than the backend's, on purpose: this one runs on a miner's home
#: connection right after a multi-gigabyte push, and a timeout here means
#: re-uploading in the miner's mind, not a retried job.
REQUEST_TIMEOUT_SEC = 30.0


class TreeError(Exception):
    """The repository listing could not be fetched, so no fingerprint could be
    computed. **Nothing has been paid at this point** -- this runs before the
    fee."""


def fetch_tree(repo_id: str, revision: str, hf_token: str = "") -> list[Any]:
    """List every file in `repo_id` at `revision`.

    The token is sent whenever there is one: the real track allows private
    repositories, and without it those answer 404 -- indistinguishable from a
    typo in the repo name.
    """
    url = f"{HF_API}/api/models/{repo_id}/tree/{revision}?recursive=true"
    headers = {"Authorization": f"Bearer {hf_token}"} if hf_token else {}
    try:
        with urlopen(build_request(url, headers), REQUEST_TIMEOUT_SEC) as response:
            payload = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        raise TreeError(_http_advice(repo_id, revision, exc.code)) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise TreeError(
            f"Could not reach HuggingFace to list {repo_id}: {exc}\n"
            f"  Your upload is not affected -- this is the listing request, and "
            f"nothing has been paid.\n"
            f"  → check your connection and run `openroboto submit` again "
            f"(it resumes: files already pushed are not pushed again)"
        ) from exc
    except json.JSONDecodeError as exc:
        raise TreeError(
            f"HuggingFace returned something that is not JSON for the file list "
            f"of {repo_id}: {exc}"
        ) from exc

    if not isinstance(payload, list):
        raise TreeError(
            f"HuggingFace returned a {type(payload).__name__} rather than a file "
            f"list for {repo_id}; cannot compute the model fingerprint"
        )
    return payload


def fetch_model_hash(repo_id: str, revision: str, hf_token: str = "") -> str:
    """The fingerprint of the weights that were just pushed.

    Returns `""` when the repository contains no LFS file at all. That is a
    **sentinel, not a fingerprint** (the protocol package says so, and the
    backend rejects it as `model_hash_empty`); the caller has to decide what it
    means, because on the real track it means the weights never arrived and the
    fee must not be paid.
    """
    entries = fetch_tree(repo_id, revision, hf_token)
    fingerprint = model_hash_from_hf_tree(
        entry for entry in entries if isinstance(entry, dict)
    )
    logger.info(
        "Model fingerprint | repo=%s files=%d hash=%s",
        repo_id,
        len(entries),
        fingerprint[:16] or "(none)",
    )
    return fingerprint


def _http_advice(repo_id: str, revision: str, code: int) -> str:
    """One message per status, because the next step really is different.

    401/403 and 404 look the same from here on a private repository -- HF
    answers 404 for a repo the token cannot see, deliberately, so as not to leak
    its existence -- so both messages mention the token.
    """
    if code in (401, 403):
        return (
            f"HuggingFace refused the file listing of {repo_id} (HTTP {code}).\n"
            f"  → check that `huggingface.token` in miner.yaml can read this "
            f"repository"
        )
    if code == 404:
        return (
            f"HuggingFace has no {repo_id} at commit {revision[:8]} "
            f"(HTTP 404).\n"
            f"  A private repository also answers 404 when the token cannot see "
            f"it, so this is either the repo name, the commit, or the token.\n"
            f"  → run `openroboto submit` again and check "
            f"`huggingface.username` / `huggingface.token`"
        )
    return (
        f"HuggingFace returned HTTP {code} for the file listing of {repo_id}. "
        f"This is their side, not your model; nothing has been paid.\n"
        f"  → try again in a few minutes"
    )
