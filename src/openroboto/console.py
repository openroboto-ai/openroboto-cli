"""Terminal output.

A command's body goes to stdout (a miner may `| jq` it or `>` it into a file);
hints and errors go to stderr. That is the whole distinction, and no more is
needed.
"""

from __future__ import annotations

import sys


def say(message: str = "") -> None:
    """Body, stdout."""
    print(message)


def hint(message: str) -> None:
    """Hint, stderr. Does not disturb the body flowing through the pipe."""
    print(message, file=sys.stderr)


def fail(message: str) -> None:
    """Error, stderr. The command layer is responsible for returning a
    non-zero exit code."""
    print(f"❌ {message}", file=sys.stderr)
