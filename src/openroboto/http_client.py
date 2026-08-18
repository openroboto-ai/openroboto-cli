"""所有 HTTP 出站请求走这里。stdlib `urllib`，不引 `requests`。

存在的理由有两个：

1. **User-Agent 只有一个来源。** 旧代码在 `rt.py` / `miner.py` / `validator.py`
   里各写了一遍 `robot-train-subnet/0.5` —— 一个写死的假版本号，服务端日志里
   分辨不出矿工手上是哪一版客户端。现在带真实版本号，且只有一处。

2. **证书。** uv / python-build-standalone 装出来的解释器
   `ssl.get_default_verify_paths().cafile` 是 `None`，任何 HTTPS 都会
   `CERTIFICATE_VERIFY_FAILED`（本机实测）。`certifi` 已经在依赖树里
   （huggingface_hub → requests → certifi），拿它兜底比让矿工去研究
   「为什么 curl 能通而 CLI 不行」便宜得多。系统解释器有自己的 CA 库，
   这时 certifi 只是等价替换，不改行为。
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
    """带 CA 的 SSL 上下文；certifi 不在就返回 None（走解释器默认）。"""
    try:
        import certifi
    except ImportError:
        return None
    return ssl.create_default_context(cafile=certifi.where())


def build_request(
    url: str, headers: dict[str, str] | None = None
) -> urllib.request.Request:
    """建一个带 UA 的 GET 请求。"""
    request = urllib.request.Request(url)
    request.add_header("User-Agent", USER_AGENT)
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    return request


def urlopen(request: urllib.request.Request, timeout: float) -> Any:
    """`urllib.request.urlopen` 加上证书上下文。"""
    return urllib.request.urlopen(
        request, timeout=timeout, context=certificate_context()
    )
