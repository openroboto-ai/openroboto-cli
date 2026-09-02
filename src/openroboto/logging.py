"""Logging assembly. Console plus a per-day file: both handlers configured in
one go.

Calling it again for the same logger name does not stack up handlers: a
miner's `validator run` is a long-running process, and stacked handlers would
print the same log line a dozen times.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime

_FORMAT = "%(asctime)s,%(msecs)03d [%(name)s] [%(levelname)s] %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

#: Our own logger namespace. Only these are protected below.
_OURS = "openroboto"

#: The guard patches `Logger.setLevel`. Installing it twice nests the wrapper,
#: and that method is called hundreds of times during startup.
_level_guard_installed = False


def _protect_our_loggers_from_bittensor() -> None:
    """🔴 **Stop bittensor from silencing this package's logs.**

    `bittensor.utils.btlogging.LoggingMachine.before_enable_default` walks
    **every logger that exists** and calls `setLevel(CRITICAL)` on the ones it
    does not own. The line `Enabling default logging (Warning level)` in the
    output is that happening, and it happens the moment a `Subtensor` is
    constructed -- long after this module has finished configuring anything.

    Measured on 2026-08-21, with `openroboto==0.1.0a1` on a clean container:

        logging.getLogger("openroboto.chain.weights").getEffectiveLevel()
          before bt.Subtensor(...)  ->  20   (INFO)
          after                     ->  50   (CRITICAL)

    For `validator run` the effect is total: `set_weights_on_chain` logs which
    uids it is about to write, whether the extrinsic succeeded, and why it
    refused when it refuses -- none of it appears. An external validator that
    starts, sets nothing for a week, and says nothing about it looks exactly
    like one that is working.

    The fix hooks `setLevel` and refuses to raise **our** loggers above the
    level the caller asked for. bittensor's own loggers are left alone; it is
    entitled to configure those. The part that overreaches is "every logger
    that exists", and this only pushes back on that.

    ⚠️ Called once from `setup_logger`. It patches a method on the `Logger`
    class, so repeated installation would nest wrappers.
    """
    global _level_guard_installed
    if _level_guard_installed:
        return
    original = logging.Logger.setLevel

    def guarded(self: logging.Logger, level: int | str) -> None:
        numeric = (
            logging.getLevelNamesMapping().get(level, logging.NOTSET)
            if isinstance(level, str)
            else level
        )
        # Only block "raise one of ours into silence". Lowering it stays
        # allowed, so `--log-level DEBUG` and any per-module tuning we do
        # ourselves still work.
        if self.name.startswith(_OURS) and numeric > logging.WARNING:
            # Deliberately no log line here: this runs inside *any* logger's
            # setLevel, including ours, so logging from it risks recursion.
            # Being blocked is not the event -- being silenced was.
            return
        original(self, level)

    logging.Logger.setLevel = guarded  # type: ignore[method-assign]
    _level_guard_installed = True


def setup_logger(
    name: str, log_dir: str = "logs", level: str = "INFO"
) -> logging.Logger:
    """Build a logger with both a console and a file handler.

    Args:
        name: Logger name, which is also the log file name prefix.
        log_dir: Log directory, relative to the current working directory.
        level: Log level name; an unrecognized value falls back to INFO.
    """
    _protect_our_loggers_from_bittensor()

    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    # A repeated call only changes the level; it does not add a handler again.
    if logger.handlers:
        return logger

    formatter = logging.Formatter(_FORMAT, datefmt=_DATE_FORMAT)

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    logger.addHandler(console)

    os.makedirs(log_dir, exist_ok=True)
    date_str = datetime.now(UTC).strftime("%Y-%m-%d")
    file_handler = logging.FileHandler(
        os.path.join(log_dir, f"{name}-{date_str}.log"), encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger
