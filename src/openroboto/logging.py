"""日志装配。控制台 + 按天切分的文件，两个 handler 一次配好。

来自旧的 `utils/logger.py`。行为保持：同名 logger 重复调用不会叠加 handler
（矿工的 `validator run` 是常驻进程，叠 handler 会让同一行日志打十几遍）。
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
    """建一个带控制台与文件两个 handler 的 logger。

    Args:
        name: logger 名，同时是日志文件名前缀。
        log_dir: 日志目录，相对当前工作目录。
        level: 日志级别名，不认识的值退回 INFO。
    """
    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    # 重复调用只改级别，不再加 handler。
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
