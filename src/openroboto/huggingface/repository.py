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

**Not** `{username}/pi05-{suffix}` — a fixed string, and one repository for the
miner's **whole career**. That pair causes two failures, and the season-scoped
name closes both:

**A fixed name goes stale and lies.** π0.5 was archived on 2026-08-31 and
LingBot-VLA 2.0 took over; every repository kept saying `pi05`. On 2026-09-02
eight queued submissions all read `<user>/pi05-…` while every one of them held a
LingBot model (`model_type` `lingbotvla`, `action_dim` 55, checked on all
eight). Three people in a row read that table and concluded miners had
submitted the wrong base model. One proposed rejecting all eight at admission:
eight paid submissions, 0.8 TAO already burned, models entirely correct.

**Seasons pile up in one directory.** `upload_folder` never deletes, so a
career-long repository is season 7 laid on top of seasons 1 through 6, and a
`.cache/` left behind by an earlier push is `LEFTOVER_UPLOAD_STATE` to
admission — a terminal rejection with the fee already gone. Files from a season
whose base model no longer applies cannot be left behind by a repository that
did not exist during that season.

⚠️ Within one season the repository still accumulates: several submissions to
the same competition share it, so `submit` still has to judge the **HF listing**
rather than this training run's output directory.

## The value comes from the competition row

`base_model_family` is read from `miner.yaml`'s `competition:` section, which
`openroboto init` wrote from the backend. A workspace without it is **refused,
not guessed** — the same rule the rest of the CLI follows for this field: a
guessed base model judges a submission somebody paid for by rules nobody chose
for that season.

⚠️ **A miner who already has a repository should set `huggingface.repo_id`.**
Otherwise the next `upload` creates a new one and re-pushes several GB. Nothing
is lost either way — submissions are located by their on-chain commitment, not
by name — but the bytes are real.
"""

from __future__ import annotations

import re

from openroboto.config.settings import ConfigError, Settings

HOTKEY_SUFFIX_LEN = 12

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
        ConfigError: something needed is missing. **No fallback to a literal
            `miner` here** — that uploads the model to `miner/pi05-miner`, a
            repository nobody will ever evaluate, and it is found out only once
            the miner has already burned TAO. Stop before any money is spent.
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
    if not family:
        # Refuse rather than fall back to a fixed word: a fixed word is exactly
        # what this module exists not to produce, and the real value is one
        # `openroboto init --refresh` away.
        missing.append(
            "competition.base_model_family (run `openroboto init --refresh`)"
        )
    if missing:
        raise ConfigError(
            "Cannot build the HF repo name, missing:\n  - "
            + "\n  - ".join(missing)
            + "\n"
            "  → add them to miner.yaml and run again, or set "
            "huggingface.repo_id to the repository you already upload to"
        )

    if not HF_NAME_OK.match(family):
        raise ConfigError(
            f"competition.base_model_family ({family!r}) has characters "
            "HuggingFace does not allow in a repository name.\n"
            "  → set huggingface.repo_id in miner.yaml to name the repository "
            "yourself; the backend fetches whatever the commitment points at"
        )

    return f"{username}/{family}-{address[-HOTKEY_SUFFIX_LEN:]}"
