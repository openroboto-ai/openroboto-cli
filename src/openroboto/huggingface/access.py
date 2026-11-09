"""Lock the weights, hand the evaluator a key -- with the miner's own token.

The problem this solves
-----------------------
A miner's checkpoint sits in a public repository, so anyone can download the
weights before the season has even been scored. Making the repository
**private** does not work: Hugging Face answers 404 to everyone but the owner,
including us, and admission then rejects the submission -- *after* the fee is
spent, because the backend lists the repository before it verifies payment.

Measured, on real repositories:

===========================  ============  =============  ==============
repository                   others read   others fetch   we fetch
===========================  ============  =============  ==============
public                       yes           yes            yes
**gated** (+ access grant)   metadata      **no**         **yes**
private                      no            no             **no**
private + gated              no            no             **no**
===========================  ============  =============  ==============

The last row is the one that decides the design: visibility is checked *before*
the gated access list, so a private repository refuses us even when we are on
that list. Granting on a repository that is not gated is refused outright --
``model ... is not gated``.

So: **gated, not private.** The weights stop being downloadable, and one call
by the owner puts the evaluator on the access list. Nobody on our side has to
accept anything, and we never hold a miner credential -- the two calls below
run on the miner's machine with the miner's own token.

What stays visible
------------------
A gated repository is listed. Its name, file names, file sizes and LFS hashes
are public, and ``README.md`` is downloadable (Hugging Face serves it to render
the model card). Only the other files -- the weights -- return 401.

That is a deliberate trade, and it is strictly better than the public
repositories used today, where the weights themselves are free to take. It is
not an equal of private: a miner who needs the *existence* of the work hidden
is not served by this, and there is no arrangement on Hugging Face that both
hides it and lets us evaluate it.

Who the key goes to
-------------------
The account comes from the season (``params.hf.grant_to``), never from a
constant here. A released CLI cannot be re-cut every time operations change an
account, and miners running an older version would go on granting access to an
address nobody reads -- the same failure the entry fee had when it was quoted
in the docs.

**A season that names no account is left completely alone**: no gating, no
grant. Turning gating on later is an operations call on that row, not a
release; `submit` reads that live row before it uploads anything.
"""

from __future__ import annotations

from typing import Any

from huggingface_hub import HfApi
from huggingface_hub.utils import HfHubHTTPError, RepositoryNotFoundError

__all__ = ["AccessError", "grant_to_of", "lock_and_grant", "prepare_gated_repository"]


class AccessError(Exception):
    """Gating or granting failed. The message says what the miner can do."""


def grant_to_of(params: dict[str, Any] | None) -> str:
    """Which account this season wants on the access list. ``""`` = none.

    ⚠️ Read off the **live** season the backend served seconds ago, not off
    `miner.yaml`: a `miner.yaml` copied at `init` time predates any change.
    """
    section = (params or {}).get("hf")
    if not isinstance(section, dict):
        return ""
    return str(section.get("grant_to") or "")


def _check_accepted_accounts(api: HfApi, repo_id: str, grant_to: str) -> set[str]:
    """Refuse unexpected grants without changing anyone's existing permissions."""
    allowed = {repo_id.split("/", 1)[0].casefold(), grant_to.casefold()}
    accepted: set[str] = set()
    try:
        for request in api.list_accepted_access_requests(repo_id):
            user = request.user
            if not isinstance(user, str) or not user:
                raise AccessError(
                    "The repository access list contains an unreadable account."
                )
            accepted.add(user.casefold())
    except HfHubHTTPError as exc:
        raise AccessError(
            f"Could not verify the accepted access list for {repo_id}: {exc}"
        ) from exc
    unexpected = accepted - allowed
    if unexpected:
        raise AccessError(
            f"{repo_id} already grants access to other accounts: "
            f"{', '.join(sorted(unexpected))}. "
            "Review those approvals on Hugging Face before retrying. "
            "The CLI did not revoke them; no content may be uploaded or paid for."
        )
    return accepted


def lock_and_grant(*, repo_id: str, grant_to: str, hf_token: str) -> None:
    """Require manual gating and only the named evaluator's explicit approval.

    The repository owner retains its inherent access. Existing unrelated grants
    are reported, never revoked. Metadata remains public; gating cannot retract
    any content already downloaded while a repository was public.
    """
    api = HfApi(token=hf_token)
    try:
        api.update_repo_settings(repo_id=repo_id, gated="manual")
        info = api.repo_info(repo_id=repo_id, repo_type="model")
        if info.private or info.gated != "manual":
            raise AccessError(
                f"Hugging Face did not confirm manual gated access for {repo_id}."
            )
    except HfHubHTTPError as exc:
        raise AccessError(
            f"could not set {repo_id} to gated: {exc}\n"
            "  The token in your config needs write access to that repository."
        ) from exc
    _check_accepted_accounts(api, repo_id, grant_to)
    owner = repo_id.split("/", 1)[0]
    if grant_to.casefold() != owner.casefold():
        try:
            api.grant_access(repo_id=repo_id, user=grant_to)
        except HfHubHTTPError as exc:
            if "already has access" not in str(exc):
                raise AccessError(
                    f"could not grant {grant_to} access to {repo_id}: {exc}"
                ) from exc
        accepted = _check_accepted_accounts(api, repo_id, grant_to)
        if grant_to.casefold() not in accepted:
            raise AccessError(
                f"Hugging Face did not confirm {grant_to}'s access to {repo_id}."
            )


def prepare_gated_repository(*, repo_id: str, grant_to: str, hf_token: str) -> None:
    """Create metadata if needed, then gate before any submitted file is uploaded.

    A private repository is never made public by the CLI: HF's private visibility
    cannot grant an external evaluator download access through the gated list.
    """
    api = HfApi(token=hf_token)
    try:
        try:
            info = api.repo_info(repo_id=repo_id, repo_type="model")
        except RepositoryNotFoundError:
            api.create_repo(
                repo_id=repo_id, repo_type="model", private=False, exist_ok=True
            )
            info = api.repo_info(repo_id=repo_id, repo_type="model")
        if info.private:
            raise AccessError(
                f"{repo_id} is private. The CLI will not make it public. "
                "Choose a repository whose gated access the official evaluator can use."
            )
    except HfHubHTTPError as exc:
        raise AccessError(
            f"Could not prepare {repo_id} for a gated upload: {exc}"
        ) from exc
    lock_and_grant(repo_id=repo_id, grant_to=grant_to, hf_token=hf_token)
