"""All outbound HTTP requests go through here. Stdlib `urllib`, no `requests`.

There are two reasons this exists:

1. **The User-Agent has a single source.** The old code wrote
   `robot-train-subnet/0.5` once each in `rt.py` / `miner.py` /
   `validator.py` -- a hardcoded fake version number, so server-side logs
   could not tell which client revision a miner was on. It now carries the
   real version number, and in exactly one place.

2. **Certificates.** On interpreters installed by uv /
   python-build-standalone, `ssl.get_default_verify_paths().cafile` is
   `None`, so any HTTPS request ends in `CERTIFICATE_VERIFY_FAILED` (measured
   on this machine). `certifi` is already in the dependency tree
   (huggingface_hub → requests → certifi), and falling back to it is far
   cheaper than making miners investigate "why does curl work but the CLI
   does not". A system interpreter has its own CA store, in which case
   certifi is merely an equivalent substitute and changes no behavior.
"""

from __future__ import annotations

import ssl
import urllib.request
from functools import lru_cache
from typing import Any

from openroboto import __version__

USER_AGENT = f"openroboto-cli/{__version__}"


@lru_cache(maxsize=1)
def certificate_context() -> ssl.SSLContext | None:
    """An SSL context with a CA bundle; returns None when certifi is absent
    (falling back to the interpreter default)."""
    try:
        import certifi
    except ImportError:
        return None
    return ssl.create_default_context(cafile=certifi.where())


def build_request(
    url: str, headers: dict[str, str] | None = None
) -> urllib.request.Request:
    """Build a GET request carrying the UA."""
    request = urllib.request.Request(url)
    request.add_header("User-Agent", USER_AGENT)
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    return request


def urlopen(request: urllib.request.Request, timeout: float) -> Any:
    """`urllib.request.urlopen` plus the certificate context."""
    return urllib.request.urlopen(
        request, timeout=timeout, context=certificate_context()
    )
