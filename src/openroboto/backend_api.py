"""后端只读 API 客户端。

只用 stdlib `urllib` —— 为了一个 GET 让矿工装 `requests` 不值得。

## 响应形状：一个信封，成功和失败结构上就不一样

```jsonc
// 成功（列表端点的 data 是数组，分页信息在 meta.page，不混进 data）
{"data": [ … ],
 "meta": {"request_id": "01H…", "generated_at": "2026-08-18T06:37:34Z",
          "page": {"total": 7, "limit": 50, "offset": 0, "has_more": false}}}

// 失败：**没有 data**
{"error": {"code": "BURN_TX_TOO_OLD", "message": "…", "retryable": false},
 "meta": {"request_id": "01H…", "generated_at": "…"}}
```

对矿工来说，这三件事因此有了答案：

- `data` / `error` 二选一 —— 不用记「`code: 0` 表示成功」这类只有文档里才有的约定；
- `error.retryable` 直接回答唯一真正要紧的问题：**要不要再烧一笔 TAO 重试。**
  基建抖动是 `true`；「你的模型格式不对」是 `false` —— 后者重试一百次也是同样的结果；
- `error.code` 是稳定机器码。措辞会改、会翻译，码不会；写脚本只许按它分支。

出错时 `meta.request_id` 会一起打出来。报障时把那一行贴给我们，我们直接就能捞到这次
请求的全部日志，不用再互相问「你什么时候敲的、敲的是什么」。

字段模型全部从 `openroboto-protocol` 装（信封、提交记录、拒绝记录），**本仓不复制
一份**：两边钉死同一个版本号之后，「后端发的形状」和「CLI 解的形状」是**同一份
声明**，而不是各抄一遍的口头约定。

## 哪些端点要 key（2026-08-17 实测，api.openroboto.ai）

| 端点 | 无 key |
|---|---|
| `GET /api/v1/scan-rejections` | 200 —— 矿工自助查被拒原因就靠它 |
| `GET /api/v1/submissions/history` | 200 |
| `GET /api/weights` | 401，验证者要带 `X-API-Key: <public_key>` |
| `GET /api/miner/{hotkey}` | 401 |

所以 `openroboto status` 走前两个：**矿工不需要任何 key 就能查自己的提交**。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
from typing import Any, TypeVar

from openroboto_protocol.schemas import (
    Contract,
    ErrorEnvelope,
    ListEnvelope,
    ScanRejection,
    SubmissionHistoryItem,
    Weights,
)

from openroboto.http_client import build_request, urlopen

REQUEST_TIMEOUT_SEC = 30
DEFAULT_LIMIT = 20

HISTORY_PATH = "/api/v1/submissions/history"
REJECTIONS_PATH = "/api/v1/scan-rejections"
WEIGHTS_PATH = "/api/weights"

KEY_HINT = (
    "\n  这个端点需要 API key —— 验证者把 control.json 里的 public_key "
    "填进 validator.yaml 的 backend.public_key"
)

#: 形状对不上时最多贴几行原始报错：pydantic 会为**每一行**列出问题，
#: 一页 20 条能刷出上百行，而前几行就足够看出是哪个字段对不上。
MAX_MISMATCH_LINES = 6

_Model = TypeVar("_Model", bound=Contract)


def retry_advice(retryable: bool) -> str:
    """「要不要重试」的措辞**只有这一份**。

    错误信封的 `error.retryable` 和被拒记录的 `reason.retryable` 都用它。
    两处说法不一致，矿工就要自己猜「到底还要不要再烧一笔」。
    """
    if retryable:
        return "这是临时故障，原样再试一次通常就好了。"
    return "重试不会有不同的结果 —— 先把上面这条原因解决掉。"


class BackendError(Exception):
    """后端请求失败。

    带着矿工自助排查要用的三样东西：稳定错误码、能不能重试、request_id。
    `__str__` 把它们展开成几行人话 —— `cli.py` 直接打这一串，不必为它另写渲染。
    """

    def __init__(
        self,
        message: str,
        *,
        code: str = "",
        retryable: bool = False,
        request_id: str = "",
    ) -> None:
        super().__init__(message)
        #: 稳定机器码。写脚本的人只允许按它分支。
        self.code = code
        #: 重试有没有意义。措辞见 `retry_advice()`。
        self.retryable = retryable
        #: 报障时贴给我们，一行就能捞到整次请求的日志。
        self.request_id = request_id

    def __str__(self) -> str:
        lines = [str(self.args[0]) if self.args else ""]
        if self.code:
            lines.append(f"  错误码: {self.code}")
        lines.append(f"  {retry_advice(self.retryable)}")
        if self.request_id:
            lines.append(f"  request_id: {self.request_id} —— 报障时把这一行发给我们")
        return "\n".join(lines)


def fetch_submissions(
    base_url: str, hotkey: str = "", limit: int = DEFAULT_LIMIT, offset: int = 0
) -> ListEnvelope[SubmissionHistoryItem]:
    """查提交历史。

    返回整个信封而不是只返回行：`meta.page.has_more` 是「你还有提交没显示出来」的
    唯一可靠答案。让调用方自己拿 `offset + len(rows) < total` 去算，这个表达式就要在
    每个列表端点上各写一遍，写错一次的表现是**静默少显示几行**，没有任何一方会报错。
    """
    raw = _get(
        base_url, HISTORY_PATH, {"hotkey": hotkey, "limit": limit, "offset": offset}
    )
    return _parse(ListEnvelope[SubmissionHistoryItem], raw, HISTORY_PATH)


def fetch_rejections(
    base_url: str, hotkey: str = "", limit: int = DEFAULT_LIMIT, offset: int = 0
) -> ListEnvelope[ScanRejection]:
    """查扫链阶段被拒的记录 —— 「上链了但队列里没有」的答案在这里。"""
    raw = _get(
        base_url, REJECTIONS_PATH, {"hotkey": hotkey, "limit": limit, "offset": offset}
    )
    return _parse(ListEnvelope[ScanRejection], raw, REJECTIONS_PATH)


def fetch_weights(base_url: str, public_key: str = "") -> Weights:
    """取当前权重 `{hotkey: 份额}`。验证者用，需要 public_key。

    **这个端点两种形状都收**（信封的 `data`，以及裸的 `{hotkey: 份额}`）：
    ADR 02 §8.5 明写 `/api/weights` 要不要套信封「建议单独裁」，至今没裁。
    猜错的代价是不对称的 —— 解不出权重 → 发不出 `set_weights` →
    **全网排放静默停摆**，而日志上只有一行 warning。多认一种形状是两行代码。
    """
    body = _decode(_get(base_url, WEIGHTS_PATH, api_key=public_key), WEIGHTS_PATH)
    data = body.get("data", body) if isinstance(body, dict) else body
    if not isinstance(data, dict):
        raise BackendError(f"{WEIGHTS_PATH} 返回了 {type(data).__name__}，期望对象")
    return {str(k): float(v) for k, v in data.items() if isinstance(v, (int, float))}


def _get(
    base_url: str,
    path: str,
    params: dict[str, Any] | None = None,
    api_key: str = "",
) -> bytes:
    """GET 一个端点，拿回原始响应体。空值参数不会被发出去。

    返回 bytes 而不是解析好的对象：信封由 pydantic 直接从 JSON 解
    （`model_validate_json`），省掉「先 json.loads 再喂进模型」这一趟往返。
    """
    query = {k: str(v) for k, v in (params or {}).items() if v not in ("", None)}
    url = f"{base_url.rstrip('/')}{path}"
    if query:
        url = f"{url}?{urllib.parse.urlencode(query)}"

    request = build_request(url, {"X-API-Key": api_key} if api_key else None)

    try:
        with urlopen(request, REQUEST_TIMEOUT_SEC) as response:
            raw: bytes = response.read()
    except urllib.error.HTTPError as exc:
        # HTTPError 本身就是响应对象，**信封在它的 body 里**。不读它就等于把
        # code / retryable / request_id 全丢掉，只剩一个光秃秃的状态码。
        raise _http_failure(url, exc) from exc
    except (OSError, urllib.error.URLError) as exc:
        raise BackendError(f"连不上后端 {url}：{exc}", retryable=True) from exc

    # 信封规则：成功一定有 data、一定没有 error。200 里带 error 是后端的 bug，
    # 但真出现时也必须当错误报 —— 把一个错误当业务数据往下传是静默错。
    failure = _error_envelope(raw)
    if failure is not None:
        raise _from_envelope(failure)
    return raw


def _http_failure(url: str, exc: urllib.error.HTTPError) -> BackendError:
    """把 4xx / 5xx 翻成矿工能照着做下一步的错误。"""
    hint = KEY_HINT if exc.code == 401 else ""

    # fp 为 None 的 HTTPError 读不出 body（连接已断，或是构造出来的）。
    failure = _error_envelope(exc.read() if exc.fp is not None else b"")
    if failure is not None:
        return _from_envelope(failure, hint)

    # 没有信封：要么后端还没升上去，要么挡在中间的网关自己吐了一页 HTML。
    # 两种都不是矿工能修的，按状态码给一个保守的 retryable。
    return BackendError(
        f"{url} 返回 HTTP {exc.code}，响应体里没有错误信封{hint}",
        retryable=exc.code >= 500 or exc.code == 429,
    )


def _error_envelope(raw: bytes) -> ErrorEnvelope | None:
    """认出错误信封；不是错误信封（含空 body、非 JSON）就返回 None。

    `ValidationError` 是 `ValueError` 的子类，所以这里不需要 import pydantic ——
    本仓对 pydantic 的依赖全部经由 `openroboto-protocol` 这一条路。
    """
    try:
        return ErrorEnvelope.model_validate_json(raw)
    except ValueError:
        return None


def _from_envelope(envelope: ErrorEnvelope, hint: str = "") -> BackendError:
    """信封里的字段照搬，一个都不重新发明。"""
    return BackendError(
        envelope.error.message + hint,
        code=envelope.error.code,
        retryable=envelope.error.retryable,
        request_id=envelope.meta.request_id,
    )


def _parse(model: type[_Model], raw: bytes, path: str) -> _Model:
    """按协议包声明的形状解一个成功响应。

    解不出来**不是矿工的错，也不该甩他一页堆栈**：这是两边装的
    `openroboto-protocol` 不是同一版，哪一边旧了都可能。
    """
    try:
        return model.model_validate_json(raw)
    except ValueError as exc:
        # pydantic 的文档链接对矿工是纯噪音，这里不往外抛。
        complaints = [
            line
            for line in str(exc).splitlines()
            if "errors.pydantic.dev" not in line
        ]
        detail = "\n  ".join(complaints[:MAX_MISMATCH_LINES])
        raise BackendError(
            f"{path} 的响应和这一版 CLI 认识的形状对不上：\n  {detail}\n"
            "  → 先 `pip install -U openroboto` 升到最新；已经是最新还这样，"
            "就是后端还没跟上，把上面这几行发给我们"
        ) from exc


def _decode(raw: bytes, path: str) -> Any:
    """成功响应必须是 JSON。不是 —— 多半是网关或代理挡在了中间，可以重试。"""
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise BackendError(f"{path} 返回的不是 JSON：{exc}", retryable=True) from exc
