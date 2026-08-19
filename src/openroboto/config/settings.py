"""`miner.yaml` / `validator.yaml` 的解析。

字段名就是矿工手里那份 YAML 的键名 —— **改一个键名，现有矿工的配置文件全部失效**
（AGENTS.md 红线 #3）。所以这里只搬家，键名一个字母都没动。

分工：
- `miner.yaml` 是**用户的**（凭据、路径、自己的训练脚本）；
- `control.json` 是**子网的**（本轮 payment / dataset / training / process），
  见 `config/control.py`。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import yaml


class ConfigError(Exception):
    """配置缺项或不合法。消息面向矿工，必须说清哪一项、期望值、怎么修。"""


@dataclass
class Settings:
    """一份配置文件的全部内容。可变 —— `apply_control()` 会就地覆盖 payment 段。"""

    # ─── Bittensor ─────────────────────────────────────
    network: str = "finney"
    # ⚠️ 这个字段目前**不生效**：链连接走 `bt.Subtensor(network=...)`，只认网络名。
    # 旧代码也一样（`utils/chain.py::get_subtensor` 的 endpoint 参数从未被使用），
    # 所以这里保持同样的行为，没有偷偷改成「连你配的节点」—— 那会改变交易发往哪个
    # 节点。要不要真正支持自定义端点，需要单独决定。
    subtensor_endpoint: str = ""
    # 0 = 未配置。旧代码默认 313（测试网），而主网是 80 —— 一个漏配 netuid 的
    # miner.yaml 会把 burn 打到另一条子网上，TAO 真烧掉。默认值救不了这种错，
    # 只能拒绝启动，所以这里不给默认网络，由 `require_for_chain()` 拦。
    netuid: int = 0
    wallet_path: str = ""
    coldkey: str = "default"
    hotkey: str = "default"
    hotkey_ss58: str = ""
    wallet_password: str = ""
    weight_interval_min: int = 720

    # ─── 公开 HTTP 资源 ────────────────────────────────
    control_json_url: str = ""
    dataset_train_url: str = ""
    dataset_val_url: str = ""
    dataset_test_url: str = ""

    # ─── 模型 ──────────────────────────────────────────
    model_cache_dir: str = "/models"
    vla_model_id: str = "pi05"
    vla_checkpoint_path: str = ""

    # ─── HuggingFace ───────────────────────────────────
    hf_token: str = ""
    hf_username: str = ""
    hf_merged_model_id: str = ""

    # ─── 日志 ──────────────────────────────────────────
    log_level: str = "INFO"
    log_dir: str = "logs"

    # ─── 矿工 ──────────────────────────────────────────
    custom_train_script: str = ""

    # ─── 验证者 ────────────────────────────────────────
    backend_url: str = "https://api.openroboto.ai"
    backend_public_key: str = ""

    # ─── 支付（正常由 control.json 覆盖） ──────────────
    #: 本轮要烧多少 TAO。**没有默认值，这是有意的。**
    #:
    #: 原先默认 `0.01`，而线上一直是 `0.1` —— 差十倍。control.json 抓不到时
    #: `refresh_burn_rate` 会退回这个值继续烧，于是网络抖一下矿工就少烧十倍，
    #: 后端按金额核对直接拒，**TAO 不退**。AGENTS.md §6 已知缺陷表里就有这条
    #: （`docs/control_json_example.json` 写着 0.01）。
    #:
    #: 现在：control.json 和 miner.yaml 都没给 → **拒绝烧**，不猜。
    #: 烧钱是不可撤销操作，fail-closed 是唯一可接受的默认（AGENTS.md §4）。
    burn_rate_tao: float | None = None
    limit_price_rao: int = 0

    #: burn 到 announce 之间允许隔多少个区块。超了后端判 `rejected`，TAO 不退。
    #:
    #: 值的依据：生产 `backend.yaml` 的 `scanner.burn_block_window`，
    #: 经 `backend/config.py:77` 读取、`scanner/burn_verify.py:71` 执行 —— **50**。
    #: （本仓文档一度写 10 个区块，2026-08-19 已按「生产行为优先」统一成 50。）
    #:
    #: 🔴 **这里违反红线 #1**：「burn 金额与区块窗口一律从 `openroboto-protocol`
    #: 装，不许在本仓复制一份」。它现在就是本仓的一份副本。
    #: 之所以先这样：协议包眼下没有这个常量，加过去是跨仓改动 + 一次发版，
    #: 而窗口检查不能继续缺着（缺它 = 矿工白烧）。
    #: **待办**：挪进 `openroboto_protocol`，本字段改成从协议包读默认值。
    #: 在那之前，改这个数字必须同时对齐生产 `backend.yaml`。
    burn_block_window: int = 50

    def require_for_chain(self) -> None:
        """上链前的最低配置检查。缺一项就直接拒绝 —— 链上操作要花钱，不能带着错配跑。"""
        missing: list[str] = []
        if self.netuid <= 0:
            missing.append("subnet.netuid（主网是 80，测试网当年是 313）")
        if not self.network:
            missing.append("subnet.network（finney | test | local）")
        if missing:
            raise ConfigError(
                "配置缺项，无法上链：\n  - " + "\n  - ".join(missing) + "\n"
                "  → 编辑 miner.yaml 补上这些字段，或 `openroboto init` 生成一份模板"
            )

    @classmethod
    def from_yaml(cls, path: str) -> Settings:
        """读一份 YAML。键名与 `miner.example.yaml` 完全一致。"""
        try:
            with open(path, encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
        except FileNotFoundError as exc:
            raise ConfigError(
                f"找不到配置文件 {path}\n"
                f"  → `openroboto init <目录名>` 会生成一份可用的 miner.yaml"
            ) from exc
        except yaml.YAMLError as exc:
            raise ConfigError(f"{path} 不是合法的 YAML：{exc}") from exc

        if not isinstance(raw, dict):
            raise ConfigError(
                f"{path} 顶层必须是映射（key: value），实际是 {type(raw).__name__}"
            )

        return cls.from_mapping(raw)

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> Settings:
        """从已经解析好的 YAML 映射构造。测试直接走这条，不用落盘。"""
        cfg = cls()

        subnet = _section(data, "subnet")
        cfg.network = subnet.get("network", cfg.network)
        cfg.subtensor_endpoint = subnet.get(
            "subtensor_endpoint", cfg.subtensor_endpoint
        )
        cfg.netuid = int(subnet.get("netuid", cfg.netuid) or 0)
        cfg.wallet_path = subnet.get("wallet_path", cfg.wallet_path)
        # coldkey / hotkey 在 YAML 里可能被解析成数字（钱包名叫 `123`），统一转字符串。
        cfg.coldkey = str(subnet.get("coldkey", cfg.coldkey))
        cfg.hotkey = str(subnet.get("hotkey", cfg.hotkey))
        cfg.hotkey_ss58 = subnet.get("hotkey_ss58", cfg.hotkey_ss58)
        cfg.wallet_password = subnet.get("wallet_password", cfg.wallet_password)

        urls = _section(data, "urls")
        cfg.control_json_url = urls.get("control_json", cfg.control_json_url)
        cfg.dataset_train_url = urls.get("dataset_train", cfg.dataset_train_url)
        cfg.dataset_val_url = urls.get("dataset_val", cfg.dataset_val_url)
        cfg.dataset_test_url = urls.get("dataset_test", cfg.dataset_test_url)

        model = _section(data, "model")
        cfg.model_cache_dir = model.get("cache_dir", cfg.model_cache_dir)
        cfg.vla_model_id = model.get("vla_model_id", cfg.vla_model_id)
        cfg.vla_checkpoint_path = model.get(
            "vla_checkpoint_path", cfg.vla_checkpoint_path
        )

        hf = _section(data, "huggingface")
        cfg.hf_token = hf.get("token", cfg.hf_token)
        cfg.hf_username = hf.get("username", cfg.hf_username)
        cfg.hf_merged_model_id = hf.get("merged_model_id", cfg.hf_merged_model_id)

        cfg.log_level = data.get("log_level", cfg.log_level)
        cfg.log_dir = data.get("log_dir", cfg.log_dir)
        cfg.weight_interval_min = int(
            data.get("weight_interval_min", cfg.weight_interval_min)
        )
        cfg.custom_train_script = data.get(
            "custom_train_script", cfg.custom_train_script
        )

        backend = _section(data, "backend")
        cfg.backend_url = backend.get("url", cfg.backend_url)
        cfg.backend_public_key = backend.get("public_key", cfg.backend_public_key)

        # miner.yaml 里的 payment 段是本地覆盖，正常情况下由 control.json 盖掉。
        payment = _section(data, "payment")
        if payment.get("burn_rate_tao") is not None:
            cfg.burn_rate_tao = float(payment["burn_rate_tao"])
        if payment.get("limit_price_rao") is not None:
            cfg.limit_price_rao = int(payment["limit_price_rao"])

        return cfg

    @classmethod
    def load(cls, path: str) -> Settings:
        """`from_yaml` 的别名，命令层统一用这个名字。"""
        return cls.from_yaml(path)


def _section(data: dict[str, Any], name: str) -> dict[str, Any]:
    """取一个二级段。段写成空（`subnet:` 后面什么都没有）时给回空字典。"""
    value = data.get(name)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"配置段 `{name}` 必须是映射，实际是 {type(value).__name__}")
    return value
