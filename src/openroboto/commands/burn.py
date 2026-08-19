"""`openroboto burn` —— 烧 TAO 付评测费（旧 `rt.py burn`）。

这是唯一一条**花钱且不可撤销**的命令，所以顺序是死的：
先刷 control.json 拿本轮费率 → 再跑上链前自检 → 最后才发交易。
自检不过就一分钱都不烧。
"""

from __future__ import annotations

import argparse
import logging
from typing import Any

from openroboto.chain import get_subtensor, open_wallet
from openroboto.config import Settings, refresh_burn_rate
from openroboto.console import fail, say
from openroboto.payment import execute_stake_burn
from openroboto.preflight import check_announce_ready, payload_size
from openroboto.round_state import load_state, resolve_round, save_state

logger = logging.getLogger("openroboto")


def add_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("burn", help="烧 TAO 付评测费")
    parser.add_argument("--config", default="miner.yaml")
    parser.add_argument("--round", type=int, default=0)
    parser.set_defaults(handler=run)


def run(args: argparse.Namespace) -> int:
    settings = Settings.load(args.config)
    round_num = resolve_round(args.round)
    state = load_state(round_num)

    if not perform_burn(settings, round_num, state):
        return 1

    say(
        f"✅ burn 完成 | tx={state['burn_tx_hash'][:16]}... block={state['burn_block']}"
    )
    say("   → 下一步 `openroboto announce`（**必须**做完，否则这笔 burn 没人看得见）")
    return 0


def perform_burn(settings: Settings, round_num: int, state: dict[str, Any]) -> bool:
    """烧一次，把 tx 与区块写进断点。返回 False 表示自检没过，什么都没花。"""
    settings.require_for_chain()
    refresh_burn_rate(settings, logger)

    # 费率没解析出来就**停在这里**，不猜。旧代码默认 0.01 而线上是 0.1：
    # control.json 抓失败时矿工少烧十倍，后端按金额核对直接拒，TAO 不退。
    # 这是烧钱前的最后一道 fail-closed 闸，别给它加兜底值。
    if settings.burn_rate_tao is None:
        # 这段提示里**不写具体金额**。费率是子网公布的、会变的，写死一个数字
        # 就是把刚删掉的 0.01 换个地方重新长出来。
        fail(
            f"拿不到 round {round_num} 的评测费率，**不会** burn。\n"
            f"   费率的唯一权威来源是子网公布的 control.json 里的"
            f" `payment.burn_rate_tao`；金额烧错后端会拒，且 TAO 不退，"
            f"所以这里不替你猜一个值。\n"
            f"   → 先跑 `openroboto doctor` 看 control.json 能不能访问。\n"
            f"   → 真要手动指定，去 control.json 抄当前值填进 miner.yaml 的"
            f" `payment.burn_rate_tao`（抄错等于白烧，务必核对）"
        )
        return False

    reasons = check_announce_ready(state, round_num)
    if reasons:
        fail(f"上链前自检没过（round {round_num}），**不会** burn：")
        for reason in reasons:
            say(f"   • {reason}")
        return False
    say(f"✅ 自检通过 | commitment payload {payload_size(state, round_num)}/512 字节")

    say(f"🔥 即将烧 {settings.burn_rate_tao} TAO（netuid={settings.netuid}，不可撤销）")

    subtensor = get_subtensor(settings.network)
    try:
        wallet = open_wallet(settings)
        receipt = execute_stake_burn(
            subtensor=subtensor,
            wallet=wallet,
            netuid=settings.netuid,
            amount_tao=settings.burn_rate_tao,
            limit_price_rao=settings.limit_price_rao,
        )
    finally:
        subtensor.close()

    state["burn_tx_hash"] = receipt.tx_hash
    state["burn_block"] = receipt.block_number
    state["step"] = "burn"
    state["status"] = "completed"
    save_state(round_num, state)
    return True
