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
grant. That is what every simulation season does today, and turning one on
later is an operations call on that row, not a release.
"""

from __future__ import annotations

from typing import Any

from huggingface_hub import HfApi
from huggingface_hub.utils import HfHubHTTPError

__all__ = ["AccessError", "grant_to_of", "lock_and_grant"]


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


def lock_and_grant(*, repo_id: str, grant_to: str, hf_token: str) -> None:
    """Make the repository gated and put `grant_to` on its access list.

    Both calls use the **miner's** token and run on the miner's machine.

    Idempotent by construction: gating an already-gated repository is a no-op,
    and Hugging Face answers "already has access" when the account is on the
    list -- which is success, not a failure, because it is the state we asked
    for. Any submit can therefore be re-run.

    Raises:
        AccessError: the repository could not be locked, or the account could
            not be added. **The caller must not spend the fee** -- a submission
            we cannot read is rejected at admission, and the fee is not
            refunded.
    """
    api = HfApi(token=hf_token)
    try:
        # `gated="manual"` and not `"auto"`: with `auto`, anyone who clicks
        # gets the weights after agreeing to the terms, which is not a lock at
        # all -- it is a click-through. `manual` means the owner decides, and
        # the call below is the owner deciding once, for us.
        api.update_repo_settings(repo_id=repo_id, gated="manual")
    except HfHubHTTPError as exc:
        raise AccessError(
            f"could not set {repo_id} to gated: {exc}\n"
            f"  The token in your config needs write access to that repository."
        ) from exc

    try:
        api.grant_access(repo_id=repo_id, user=grant_to)
    except HfHubHTTPError as exc:
        if "already has access" in str(exc):
            return
        raise AccessError(
            f"could not grant {grant_to} access to {repo_id}: {exc}\n"
            f"  Without it the evaluator cannot download your weights, and the\n"
            f"  submission is rejected after the fee is spent."
        ) from exc
