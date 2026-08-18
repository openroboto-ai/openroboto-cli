"""终端输出。

命令的正文走 stdout（矿工可能 `| jq` 或者 `>` 到文件），
提示与错误走 stderr。就这么点区别，不需要更多。
"""

from __future__ import annotations

import sys


def say(message: str = "") -> None:
    """正文，stdout。"""
    print(message)


def hint(message: str) -> None:
    """提示，stderr。不影响管道里的正文。"""
    print(message, file=sys.stderr)


def fail(message: str) -> None:
    """错误，stderr。命令层负责返回非零退出码。"""
    print(f"❌ {message}", file=sys.stderr)
