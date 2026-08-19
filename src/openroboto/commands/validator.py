"""`openroboto validator run` —— 外部验证者常驻进程（旧 `validator.py`）。

验证者**不跑评测**：后端算好权重，它只负责读回来设到链上。
control.json 每轮会刷新 `public_key`，所以循环里顺带更新它 ——
key 轮换时验证者不用重启。

旧循环里每 60 秒调一次 `scan_chain_submissions()` 但**返回值一处都没用** ——
一次全量 metagraph 同步加上逐个 hotkey 读 commitment，纯粹白烧 RPC。这次去掉。
"""

from __future__ import annotations

import argparse
import logging
import time

from openroboto.backend_api import BackendError, fetch_weights
from openroboto.chain import (
    get_metagraph,
    get_subtensor,
    open_wallet,
    set_weights_on_chain,
)
from openroboto.config import ControlFetchError, Settings, apply_control, fetch_control
from openroboto.console import say

logger = logging.getLogger("openroboto.validator")

POLL_INTERVAL_SEC = 60


def add_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("validator", help="外部验证者")
    inner = parser.add_subparsers(dest="validator_command", required=True)

    run_parser = inner.add_parser("run", help="常驻：读后端权重并设到链上")
    run_parser.add_argument("--config", default="validator.yaml")
    run_parser.add_argument(
        "--once", action="store_true", help="只跑一轮就退出（cron / 调试用）"
    )
    run_parser.set_defaults(handler=run)


def run(args: argparse.Namespace) -> int:
    settings = Settings.load(args.config)
    settings.require_for_chain()

    say(f"验证者启动 | network={settings.network} netuid={settings.netuid}")
    say(f"  后端: {settings.backend_url}")
    say(f"  设权重间隔: {settings.weight_interval_min} 分钟")

    subtensor = get_subtensor(settings.network)
    wallet = open_wallet(settings)

    weight_interval_sec = settings.weight_interval_min * 60
    last_weight_set = 0.0
    control_etag = ""
    public_key = settings.backend_public_key

    while True:
        try:
            if settings.control_json_url:
                fetched = fetch_control(settings.control_json_url, control_etag)
                control_etag = fetched.etag
                if fetched.control is not None:
                    apply_control(settings, fetched.control)
                    new_key = fetched.control.get("public_key", "")
                    if isinstance(new_key, str) and new_key and new_key != public_key:
                        logger.info("control.json 里的 public_key 已更新")
                        public_key = new_key

            now = time.time()
            if now - last_weight_set >= weight_interval_sec:
                if _set_weights_once(settings, subtensor, wallet, public_key):
                    last_weight_set = now
        except (BackendError, ControlFetchError) as exc:
            # 基建故障：后端抖动 / control.json 拉不到。常驻进程不该因此退出。
            logger.warning("本轮跳过：%s", exc)
        except Exception as exc:  # 未知异常同样不能让常驻进程死掉
            logger.error("循环异常：%s", exc, exc_info=True)

        if args.once:
            return 0
        logger.info("%d 秒后再看一次", POLL_INTERVAL_SEC)
        time.sleep(POLL_INTERVAL_SEC)


def _set_weights_once(
    settings: Settings, subtensor: object, wallet: object, public_key: str
) -> bool:
    """取一次权重并设到链上。返回是否真的设成功。"""
    weights = fetch_weights(settings.backend_url, public_key)
    if not weights:
        logger.warning("后端没有给出权重，本轮不设")
        return False

    metagraph = get_metagraph(settings.netuid, settings.network, subtensor)
    return set_weights_on_chain(
        subtensor, wallet, settings.netuid, weights, list(metagraph.hotkeys)
    )
