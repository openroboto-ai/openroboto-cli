"""Derivation of the HuggingFace repository name.

The format `{username}/pi05-{last 12 chars of hotkey_ss58}` is **part of the public
protocol**: when the backend scans the chain it takes the `i` field out of the
commitment and pulls that repository straight from HF — `kyleab/pi05-qXgcGfvRk2Xp`
on the live leaderboard is exactly that. Changing the format = changing the
protocol.
"""

from __future__ import annotations

from openroboto.config.settings import ConfigError, Settings

HOTKEY_SUFFIX_LEN = 12


def build_repo_id(settings: Settings, hotkey_ss58: str = "") -> str:
    """Assemble the HF repository id this miner machine should upload to.

    Args:
        settings: takes `huggingface.username` and `subnet.hotkey_ss58`.
        hotkey_ss58: explicit override (for example the address read from the
            wallet).

    Raises:
        ConfigError: the username or the hotkey address is missing. The old code
            fell back to the literal `miner` here, which uploaded the model to
            `miner/pi05-miner` — a repository nobody will ever evaluate, and by
            that point the miner had already burned TAO. Better to stop before any
            money is spent.
    """
    username = settings.hf_username
    address = hotkey_ss58 or settings.hotkey_ss58

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
            "  \u2192 add them to miner.yaml and run again; the submitted repo "
            "name is part of the protocol, it cannot be approximated"
        )

    return f"{username}/pi05-{address[-HOTKEY_SUFFIX_LEN:]}"
