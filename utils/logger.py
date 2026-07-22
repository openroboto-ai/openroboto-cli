"""
Logging setup for the subnet.
"""

import os
import logging
from datetime import datetime, timezone

def setup_logger(name: str, log_dir: str = "logs", level: str = "DEBUG") -> logging.Logger:
    """Create a logger with file + console handlers."""
    os.makedirs(log_dir, exist_ok=True)

    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Prevent duplicate handlers on reload
    if logger.handlers:
        return logger

    formatter = logging.Formatter(
        "%(asctime)s,%(msecs)03d [%(name)s] [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Console handler
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    logger.addHandler(console)

    # File handler (per-day)
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    file_path = os.path.join(log_dir, f"{name}-{date_str}.log")
    file_handler = logging.FileHandler(file_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger
