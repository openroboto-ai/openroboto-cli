"""Derivation of the HuggingFace repository name.

The name travels to the backend inside the commitment's `i` field and the
backend pulls whatever string is there, so this is a **default, not a protocol
rule**. That is not a reading of the code, it is on the live leaderboard:
`edward56/lingbot-sn80-CqAkpFWmxR1X` was submitted, evaluated and scored, and it
matches no format this module has ever produced. Nothing in the backend or in
`openroboto-protocol` branches on the name.

## 🔴 One repository per season, named after that season's base model

`{username}/{base_model_family}-{last 12 of hotkey}`, e.g.
`kyleab/lingbot-vla-2.0-qXgcGfvRk2Xp`.

It used to be `{username}/pi05-{suffix}` — a fixed string, and one repository
for the miner's **whole career**. Two failures came out of that pair, and the
season-scoped name closes both:

**The name went stale and lied.** π0.5 was archived on 2026-08-31 and
LingBot-VLA 2.0 took over; every repository kept saying `pi05`. On 2026-09-02
eight queued submissions all read `<user>/pi05-…` while every one of them held a
LingBot model (`model_type` `lingbotvla`, `action_dim` 55, checked on all
eight). Three people in a row read that table and concluded miners had
submitted the wrong base model. One proposed rejecting all eight at admission:
eight paid submissions, 0.8 TAO already burned, models entirely correct.

**Seasons piled up in one directory.** `upload_folder` never deletes, so a
career-long repository is season 7 laid on top of seasons 1 through 6, and a
`.cache/` left behind by an earlier push is `LEFTOVER_UPLOAD_STATE` to
admission — a terminal rejection with the fee already gone. Files from a season
whose base model no longer applies cannot be left behind by a repository that
did not exist during that season.

⚠️ Within one season the repository still accumulates: several submissions to
the same competition share it, so `submit` still has to judge the **HF listing**
rather than this round's output directory.

## The value comes from the competition row

`base_model_family` is read from `miner.yaml`'s `competition:` section, which
`openroboto init` writes from the backend.

🔴 **A workspace without it keeps the historical name** (`LEGACY_PREFIX`), it is
not refused. Every workspace created by a released CLI is in that state:
`base_model_family` reached `SECTION_KEYS` after 1.1.1 shipped, so **no miner
running today has that key**.

The first version of this module refused instead, on the grounds that the rest
of the CLI never guesses this field. That reasoning was borrowed from a
different use and does not survive the move:

| `base_model_family` used as | guessing it wrong |
|---|---|
| which rule book judges a checkpoint | judges a paid submission by rules |
| | nobody chose for it — **never guess** |
| **a word in a repository name** | decides nothing; the backend fetches |
| | whatever the commitment points at |

And for the workspaces in question it is not a guess at all: their repository is
*already* named `pi05-…`, so keeping that name is simply telling the truth about
where their model lives.

Refusing would have made 1.2.0 break every miner on upgrade — `upload` dies
until they run `init --refresh`, and then re-pushes ~25 GB to a new repository
nobody asked for. Safely (the failure lands before any TAO is burned) but for no
gain: the season-scoped name is worth having for new workspaces and worth
nothing when forced on old ones.

⚠️ **To move an existing repository to the season-scoped name**, run
`openroboto init --refresh` — the next upload re-pushes the model once. To pin
any repository explicitly, set `huggingface.repo_id`.
"""

from __future__ import annotations

import re

from openroboto.config.settings import ConfigError, Settings

HOTKEY_SUFFIX_LEN = 12

#: The prefix a workspace keeps when its season does not name a base model.
#:
#: 🔴 **This is not a claim about the model.** It is the name those repositories
#: already have — `{user}/pi05-{suffix}` was the fixed format until 2026-09-02 —
#: so using it for a workspace that predates the change points at the repository
#: the miner actually uploads to. Changing it would move their repository, not
#: correct a statement.
LEGACY_PREFIX = "pi05"

#: What HuggingFace accepts in the repository part of a repo id.
#:
#: Checked rather than sanitised: a name we quietly rewrite is a name the miner
#: cannot predict, and they need to find their own repository on the website.
#: A `base_model_family` that does not fit belongs in `huggingface.repo_id`,
#: where they choose the name themselves.
HF_NAME_OK = re.compile(r"^[A-Za-z0-9._-]+$")


def build_repo_id(settings: Settings, hotkey_ss58: str = "") -> str:
    """Assemble the HF repository id this miner machine should upload to.

    `huggingface.repo_id` wins when set, verbatim and unvalidated. A miner who
    writes down a repository has already said where their model goes; second-
    guessing that string here can only push the upload somewhere they did not
    ask for.

    Args:
        settings: takes `huggingface.repo_id`, `huggingface.username`,
            `competition.base_model_family` and `subnet.hotkey_ss58`.
        hotkey_ss58: explicit override (for example the address read from the
            wallet).

    Raises:
        ConfigError: something needed is missing. The old code fell back to the
            literal `miner` here, which uploaded the model to `miner/pi05-miner`
            — a repository nobody will ever evaluate, and by that point the miner
            had already burned TAO. Better to stop before any money is spent.
    """
    if settings.hf_repo_id:
        return settings.hf_repo_id

    username = settings.hf_username
    address = hotkey_ss58 or settings.hotkey_ss58
    family = settings.competition_base_model_family

    missing: list[str] = []
    if not username:
        missing.append("huggingface.username")
    if not address:
        missing.append("subnet.hotkey_ss58 (or a wallet the hotkey can be read from)")
    if missing:
        raise ConfigError(
            "Cannot build the HF repo name, missing:\n  - "
            + "\n  - ".join(missing)
            + "\n"
            "  → add them to miner.yaml and run again, or set "
            "huggingface.repo_id to the repository you already upload to"
        )

    if not family:
        # No season base model on record → this workspace predates the change,
        # and its repository is already named this. See LEGACY_PREFIX.
        return f"{username}/{LEGACY_PREFIX}-{address[-HOTKEY_SUFFIX_LEN:]}"

    if not HF_NAME_OK.match(family):
        raise ConfigError(
            f"competition.base_model_family ({family!r}) has characters "
            "HuggingFace does not allow in a repository name.\n"
            "  → set huggingface.repo_id in miner.yaml to name the repository "
            "yourself; the backend fetches whatever the commitment points at"
        )

    return f"{username}/{family}-{address[-HOTKEY_SUFFIX_LEN:]}"
