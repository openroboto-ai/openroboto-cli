"""control.json 的抓取与应用 —— **全仓唯一一处实现**。

搬家之前这段逻辑在 `validator.py` / `miner.py` / `rt.py` 里各写了一遍：
三份 User-Agent、三套 ETag 处理、三种失败时的兜底。`rt.py` 那份还把
`burn_rate_tao` 的兜底值写成 0.01，而线上是 0.1 —— 抓取失败时矿工会**少烧十倍**，
后端按金额核对直接拒，TAO 照样没了。收敛成一处之后，兜底值只有一个来源：
`Settings.burn_rate_tao`（miner.yaml 里的值），不再有第二个字面量。

control.json **只承载 payment / dataset / training / process**，
它不是后端配置源（见 openroboto-backend/docs/adr/01）。
"""

from __future__ import annotations

import json
import urllib.error
from dataclasses import dataclass
from typing import Any

from openroboto.config.settings import Settings
from openroboto.http_client import build_request, urlopen

FETCH_TIMEOUT_SEC = 30


class ControlFetchError(Exception):
    """control.json 拉不下来 / 解不开。

    这是**基建故障**，不是矿工配错了。调用方要按基建故障处理：
    train 停下（没有 round 号就没法训），burn / validator 退回本地配置继续。
    """


@dataclass(frozen=True)
class ControlFetch:
    """一次抓取的结果。"""

    control: dict[str, Any] | None
    """control.json 的内容；`None` 表示服务端回了 304，内容没变。"""

    etag: str
    """本次的 ETag，下次带上去省流量。服务端不给就沿用上一次的。"""


def fetch_control(url: str, etag: str = "") -> ControlFetch:
    """HTTP 抓 control.json，支持 ETag 条件请求。

    Args:
        url: control.json 的直链。
        etag: 上一次拿到的 ETag，为空则无条件抓。

    Raises:
        ControlFetchError: 网络错误、超时、或返回的不是合法 JSON 对象。
    """
    request = build_request(url, {"If-None-Match": etag} if etag else None)

    try:
        with urlopen(request, FETCH_TIMEOUT_SEC) as response:
            new_etag = response.headers.get("ETag", "").strip('"') or etag
            # urllib 对 304 会抛 HTTPError（见下），这一支是少数服务端
            # 直接回 304 body 的情况。
            if response.status == 304:
                return ControlFetch(None, new_etag)
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            return ControlFetch(None, etag)
        raise ControlFetchError(f"control.json 返回 HTTP {exc.code}：{url}") from exc
    except (OSError, urllib.error.URLError) as exc:
        raise ControlFetchError(f"control.json 拉取失败（网络问题）：{exc}") from exc
    except json.JSONDecodeError as exc:
        raise ControlFetchError(f"control.json 不是合法 JSON：{exc}") from exc

    if not isinstance(payload, dict):
        raise ControlFetchError(
            f"control.json 顶层必须是对象，实际是 {type(payload).__name__}"
        )
    return ControlFetch(payload, new_etag)


def apply_control(settings: Settings, control: dict[str, Any]) -> None:
    """把 control.json 里子网说了算的字段盖到 settings 上。

    只有 `payment` 与 `training` 两段会改 settings：
    - `payment.burn_rate_tao` / `payment.limit_price_rao` —— 本轮费率，必须听子网的；
    - `training.vla_checkpoint_path` / `training.vla_model_id` —— 基座模型。

    `dataset` 与 `process` 段由 train 命令直接读，不进 settings（它们是每轮变的
    输入，不是配置）。
    """
    payment = control.get("payment") or {}
    if isinstance(payment, dict):
        if payment.get("burn_rate_tao") is not None:
            settings.burn_rate_tao = float(payment["burn_rate_tao"])
        if payment.get("limit_price_rao") is not None:
            settings.limit_price_rao = int(payment["limit_price_rao"])

    training = control.get("training") or {}
    if isinstance(training, dict):
        if training.get("vla_checkpoint_path"):
            settings.vla_checkpoint_path = str(training["vla_checkpoint_path"])
        if training.get("vla_model_id"):
            settings.vla_model_id = str(training["vla_model_id"])


def refresh_burn_rate(settings: Settings, logger: Any) -> None:
    """burn 之前刷一次费率。抓不到就用 miner.yaml 里的值继续，并明确说出来。

    烧多了不退、烧少了后端按金额拒（也不退）—— 所以这行日志必须让矿工看见
    自己实际会烧多少。
    """
    if not settings.control_json_url:
        logger.warning(
            "未配置 urls.control_json，本次按 miner.yaml 的 burn_rate_tao=%s TAO 烧。"
            "费率对不上会被后端拒且不退款",
            settings.burn_rate_tao,
        )
        return
    try:
        fetched = fetch_control(settings.control_json_url)
    except ControlFetchError as exc:
        logger.warning(
            "control.json 拉取失败（%s），退回 miner.yaml 的 burn_rate_tao=%s TAO",
            exc,
            settings.burn_rate_tao,
        )
        return
    if fetched.control is not None:
        apply_control(settings, fetched.control)
    logger.info("burn_rate_tao=%s TAO（来自 control.json）", settings.burn_rate_tao)
