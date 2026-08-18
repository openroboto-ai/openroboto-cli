"""HuggingFace 仓库名的推导。

格式 `{username}/pi05-{hotkey_ss58 后 12 位}` 是**公开协议的一部分**：
后端扫链拿到 commitment 里的 `i` 字段直接去 HF 拉这个仓库，
线上榜单上的 `kyleab/pi05-qXgcGfvRk2Xp` 就是这么来的。改格式 = 改协议。
"""

from __future__ import annotations

from openroboto.config.settings import ConfigError, Settings

HOTKEY_SUFFIX_LEN = 12


def build_repo_id(settings: Settings, hotkey_ss58: str = "") -> str:
    """拼出这台矿机应该上传到的 HF 仓库 id。

    Args:
        settings: 取 `huggingface.username` 与 `subnet.hotkey_ss58`。
        hotkey_ss58: 显式覆盖（例如从钱包读出来的地址）。

    Raises:
        ConfigError: 用户名或 hotkey 地址缺失。旧代码在这里退回字面量
            `miner`，结果是把模型传到 `miner/pi05-miner` —— 一个谁都不会去评测的
            仓库，而矿工那时已经烧过 TAO 了。宁可在花钱之前停下来。
    """
    username = settings.hf_username
    address = hotkey_ss58 or settings.hotkey_ss58

    missing: list[str] = []
    if not username:
        missing.append("huggingface.username")
    if not address:
        missing.append("subnet.hotkey_ss58（或用能读出 hotkey 的钱包）")
    if missing:
        raise ConfigError(
            "拼不出 HF 仓库名，缺：\n  - " + "\n  - ".join(missing) + "\n"
            "  → 补进 miner.yaml 后重跑；提交的仓库名是协议的一部分，不能凑合"
        )

    return f"{username}/pi05-{address[-HOTKEY_SUFFIX_LEN:]}"
