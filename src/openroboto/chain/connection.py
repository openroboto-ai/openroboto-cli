"""与 Bittensor 链的连接、钱包加载、metagraph 同步。

`bittensor` 一律**在函数体内 import**：它拖着 torch 和 substrate 栈，
而 `openroboto init` / `check` / `status` 这些命令根本不碰链。
矿工在一台没装 bittensor 的机器上跑 `openroboto check` 应该照样能用。
"""

from __future__ import annotations

import getpass
import logging
import os
from typing import Any

from openroboto.config.settings import Settings

logger = logging.getLogger(__name__)


class ChainError(Exception):
    """链交互失败。属于基建故障，不要报成矿工配错。"""


def get_subtensor(network: str) -> Any:
    """建一个 subtensor 连接。"""
    import bittensor as bt

    logger.info("连接 subtensor | network=%s", network)
    return bt.Subtensor(network=network)


def get_wallet(
    coldkey: str = "default",
    hotkey: str = "default",
    path: str = "",
    password: str = "",
) -> Any:
    """加载钱包。给了密码就绕过交互式输入。

    绕过的方式是把 `getpass.getpass` 换成一个常量函数 —— bittensor SDK 内部
    就是调它读密码的，没有公开参数可传。这是 SDK 的限制，不是我们想这么干。
    """
    import bittensor as bt

    try:
        if path:
            wallet = bt.Wallet(path=str(path), name=str(coldkey), hotkey=str(hotkey))
        else:
            wallet = bt.Wallet(name=str(coldkey), hotkey=str(hotkey))
    except Exception as exc:  # SDK 抛什么类型不稳定
        raise ChainError(
            f"钱包加载失败（coldkey={coldkey} hotkey={hotkey}）：{exc}"
        ) from exc

    if not wallet.hotkey_str:
        raise ChainError(
            f"hotkey `{hotkey}` 在钱包目录 {path or '默认路径'} 里不存在或为空\n"
            f"  → 用 `btcli wallet list` 确认名字拼写"
        )

    if password:
        os.environ["BT_WALLET_PASSWORD"] = password
        getpass.getpass = lambda prompt="", stream=None: password
        logger.info("钱包密码已注入（不打印内容）")

    logger.info("钱包已加载 | hotkey_str=%s", wallet.hotkey_str)
    return wallet


def get_metagraph(netuid: int, network: str, subtensor: Any = None) -> Any:
    """取 metagraph。传了 subtensor 就同步一次。"""
    import bittensor as bt

    meta = bt.Metagraph(netuid=netuid, network=network, sync=False)
    if subtensor is not None:
        meta.sync(subtensor=subtensor)
    return meta


def open_wallet(settings: Settings) -> Any:
    """按配置加载钱包。密码没配就交给 SDK 自己问。

    旧实现（`rt.py::_read_wallet_password`）自己搭了一套交互式输入：子线程 +
    60 秒超时 + 三次重试 + 「验证密码」。那段代码**从来没跑通过** —— 验证那一支
    引用了一个不存在的变量（`password=password`），任何一次交互输入都是 `NameError`，
    只有在 miner.yaml 里写死 `wallet_password` 才能用。

    而且它验证不了什么：`bt.Wallet(...)` 只是打开文件，coldkey 要到签名时才解密。
    所以这里整段删掉，不重造 —— SDK 自己会在需要签名时提示输入并校验，
    它的报错也比我们转述得准。
    """
    return get_wallet(
        settings.coldkey,
        settings.hotkey,
        settings.wallet_path,
        settings.wallet_password,
    )
