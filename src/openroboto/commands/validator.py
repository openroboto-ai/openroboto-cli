"""`openroboto validator run` -- the external validator's long-running process
(the old `validator.py`).

A validator **does not run evaluations**: the backend computes the weights,
and it is only responsible for reading them back and setting them on chain.
control.json refreshes `public_key` every round, so the loop updates it along
the way -- a validator does not have to be restarted when the key rotates.

🔴 **`public_key` is the only thing this loop takes out of that file**, and it is
the whole reason the URL has to keep answering: it is an external validator's
only channel to the key, and their code is not ours to upgrade. The rest of the
file is a miner-side artifact on its way out; the `payment` block in particular
used to be applied to `Settings` on every cycle, which set a burn rate on a
process that never burns anything.

The old loop called `scan_chain_submissions()` every 60 seconds but **used the
return value in exactly zero places** -- a full metagraph sync plus reading the
commitment of every hotkey one by one, pure wasted RPC. It is removed here.
"""

from __future__ import annotations

import argparse
import logging
import time

from openroboto.backend_api import BackendError, fetch_weights
from openroboto.chain import (
    get_metagraph,
    get_subtensor,
    open_wallet,
    set_weights_on_chain,
)
from openroboto.config import ControlFetchError, Settings, fetch_control
from openroboto.console import say

logger = logging.getLogger("openroboto.validator")

POLL_INTERVAL_SEC = 60


def add_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("validator", help="External validator")
    inner = parser.add_subparsers(dest="validator_command", required=True)

    run_parser = inner.add_parser(
        "run", help="Long-running: read weights from the backend and set them on chain"
    )
    run_parser.add_argument("--config", default="validator.yaml")
    run_parser.add_argument(
        "--once", action="store_true", help="Run one cycle and exit (cron / debugging)"
    )
    run_parser.set_defaults(handler=run)


def run(args: argparse.Namespace) -> int:
    settings = Settings.load(args.config)
    settings.require_for_chain()

    say(f"validator started | network={settings.network} netuid={settings.netuid}")
    say(f"  backend: {settings.backend_url}")
    say(f"  weight-setting interval: {settings.weight_interval_min} min")

    subtensor = get_subtensor(settings.network)
    wallet = open_wallet(settings)

    weight_interval_sec = settings.weight_interval_min * 60
    last_weight_set = 0.0
    control_etag = ""
    public_key = settings.backend_public_key

    while True:
        try:
            if settings.control_json_url:
                fetched = fetch_control(settings.control_json_url, control_etag)
                control_etag = fetched.etag
                if fetched.control is not None:
                    new_key = fetched.control.get("public_key", "")
                    if isinstance(new_key, str) and new_key and new_key != public_key:
                        logger.info("public_key in control.json has been updated")
                        public_key = new_key

            now = time.time()
            if now - last_weight_set >= weight_interval_sec:
                if _set_weights_once(settings, subtensor, wallet, public_key):
                    last_weight_set = now
        except (BackendError, ControlFetchError) as exc:
            # Infrastructure failure: the backend flapped, or control.json
            # could not be fetched. A long-running process must not exit for
            # that.
            logger.warning("skipping this cycle: %s", exc)
        except Exception as exc:  # an unknown exception must not kill it either
            logger.error("loop error: %s", exc, exc_info=True)

        if args.once:
            return 0
        logger.info("checking again in %d seconds", POLL_INTERVAL_SEC)
        time.sleep(POLL_INTERVAL_SEC)


def _set_weights_once(
    settings: Settings, subtensor: object, wallet: object, public_key: str
) -> bool:
    """Fetch the weights once and set them on chain. Returns whether they were
    actually set successfully."""
    weights = fetch_weights(settings.backend_url, public_key)
    if not weights:
        logger.warning("the backend returned no weights; not setting any this cycle")
        return False

    metagraph = get_metagraph(settings.netuid, settings.network, subtensor)
    return set_weights_on_chain(
        subtensor, wallet, settings.netuid, weights, list(metagraph.hotkeys)
    )
