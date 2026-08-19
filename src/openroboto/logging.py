"""Logging assembly. Console plus a per-day file: both handlers configured in
one go.

Comes from the old `utils/logger.py`. The behavior is preserved: calling it
again for the same logger name does not stack up handlers (a miner's
`validator run` is a long-running process, and stacked handlers would print
the same log line a dozen times).
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime

_FORMAT = "%(asctime)s,%(msecs)03d [%(name)s] [%(levelname)s] %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def setup_logger(
    name: str, log_dir: str = "logs", level: str = "INFO"
) -> logging.Logger:
    """Build a logger with both a console and a file handler.

    Args:
        name: Logger name, which is also the log file name prefix.
        log_dir: Log directory, relative to the current working directory.
        level: Log level name; an unrecognized value falls back to INFO.
    """
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
