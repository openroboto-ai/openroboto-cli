"""Bittensor chain connection, wallet loading, metagraph sync.

`bittensor` is **always imported inside the function body**: it drags in torch and
the substrate stack, while `openroboto init` / `check` / `status` never touch the
chain at all. A miner running `openroboto check` on a machine without bittensor
installed should still work.
"""

from __future__ import annotations

import getpass
import logging
import os
from typing import Any

from openroboto.config.settings import Settings

logger = logging.getLogger(__name__)


class ChainError(Exception):
    """Chain interaction failed.

    This is an infrastructure failure, not a miner misconfiguration — do not
    report it as one.
    """


def get_subtensor(network: str) -> Any:
    """Open a subtensor connection."""
    import bittensor as bt

    logger.info("Connecting to subtensor | network=%s", network)
    return bt.Subtensor(network=network)


def get_wallet(
    coldkey: str = "default",
    hotkey: str = "default",
    path: str = "",
    password: str = "",
) -> Any:
    """Load the wallet. If a password is given, bypass the interactive prompt.

    The bypass works by replacing `getpass.getpass` with a constant function —
    that is what the bittensor SDK calls internally to read the password, and there
    is no public parameter to pass it in. This is a limitation of the SDK, not
    something we want to do.
    """
    import bittensor as bt

    try:
        if path:
            wallet = bt.Wallet(path=str(path), name=str(coldkey), hotkey=str(hotkey))
        else:
            wallet = bt.Wallet(name=str(coldkey), hotkey=str(hotkey))
    except Exception as exc:  # the exception type the SDK raises is not stable
        raise ChainError(
            f"Failed to load the wallet (coldkey={coldkey} hotkey={hotkey}): {exc}"
        ) from exc

    if not wallet.hotkey_str:
        raise ChainError(
            f"hotkey `{hotkey}` is missing or empty in the wallet directory "
            f"{path or 'default path'}\n"
            f"  \u2192 run `btcli wallet list` to check the spelling"
        )

    if password:
        os.environ["BT_WALLET_PASSWORD"] = password
        getpass.getpass = lambda prompt="", stream=None: password
        logger.info("Wallet password injected (contents are never logged)")

    logger.info("Wallet loaded | hotkey_str=%s", wallet.hotkey_str)
    return wallet


def get_metagraph(netuid: int, network: str, subtensor: Any = None) -> Any:
    """Get the metagraph. If a subtensor is passed, sync once."""
    import bittensor as bt

    meta = bt.Metagraph(netuid=netuid, network=network, sync=False)
    if subtensor is not None:
        meta.sync(subtensor=subtensor)
    return meta


def open_wallet(settings: Settings) -> Any:
    """Load the wallet from config. If no password is configured, let the SDK ask.

    🔴 **No interactive password prompt of our own** — no worker thread, no
    timeout, no retries, no "verify the password" step. There is nothing here that
    could verify one anyway: `bt.Wallet(...)` only opens files, and the coldkey is
    not decrypted until signing time. The SDK prompts and validates when a
    signature is needed, and its error messages are more accurate than our
    paraphrase of them.
    """
    return get_wallet(
        settings.coldkey,
        settings.hotkey,
        settings.wallet_path,
        settings.wallet_password,
    )
