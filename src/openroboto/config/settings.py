"""Parsing of `miner.yaml` / `validator.yaml`.

The field names are the key names in the YAML file the miner is holding —
**change one key name and every existing miner's config file stops working**
(AGENTS.md red line #3). So this was a move and nothing else: not one letter of any
key name was touched.

Division of labour:
- `miner.yaml` belongs to **the user** (credentials, paths, their own training
  script);
- `control.json` belongs to **the subnet** (this round's payment / dataset /
  training / process), see `config/control.py`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import yaml


class ConfigError(Exception):
    """A config item is missing or invalid.

    The message is aimed at the miner and must state which item, what value was
    expected, and how to fix it.
    """


@dataclass
class Settings:
    """The full content of one config file.

    Mutable — `apply_control()` overwrites the payment section in place.
    """

    # ─── Bittensor ─────────────────────────────────────
    network: str = "finney"
    # ⚠️ This field currently **has no effect**: the chain connection goes through
    # `bt.Subtensor(network=...)`, which only accepts a network name. The old code
    # behaved the same way (the endpoint parameter of `utils/chain.py::get_subtensor`
    # was never used), so the same behaviour is kept here, rather than quietly
    # changing it to "connect to the node you configured" — that would change which
    # node transactions are sent to. Whether to really support a custom endpoint
    # needs a separate decision.
    subtensor_endpoint: str = ""
    # 0 = not configured. The old code defaulted to 313 (testnet) while mainnet is
    # 80 — a miner.yaml that forgets to set netuid would send the burn to a
    # different subnet, and the TAO really is burned. A default value cannot save
    # you from that mistake, only refusing to start can, so no default network is
    # given here and `require_for_chain()` blocks it.
    netuid: int = 0
    wallet_path: str = ""
    coldkey: str = "default"
    hotkey: str = "default"
    hotkey_ss58: str = ""
    wallet_password: str = ""
    weight_interval_min: int = 720

    # ─── Public HTTP resources ─────────────────────────
    control_json_url: str = ""
    dataset_train_url: str = ""
    dataset_val_url: str = ""
    dataset_test_url: str = ""

    # ─── Model ─────────────────────────────────────────
    vla_model_id: str = "pi05"
    vla_checkpoint_path: str = ""

    # ─── HuggingFace ───────────────────────────────────
    hf_token: str = ""
    hf_username: str = ""
    hf_merged_model_id: str = ""

    # ─── Logging ───────────────────────────────────────
    log_level: str = "INFO"
    log_dir: str = "logs"

    # ─── Miner ─────────────────────────────────────────
    custom_train_script: str = ""

    # ─── Validator ─────────────────────────────────────
    backend_url: str = "https://api.openroboto.ai"
    backend_public_key: str = ""

    # ─── Payment (normally overridden by control.json) ─
    #: How much TAO to burn this round. **There is no default, and that is
    #: deliberate.**
    #:
    #: It used to default to `0.01` while production has always been `0.1` — a
    #: factor of ten. When control.json could not be fetched, `refresh_burn_rate`
    #: would fall back to this value and burn anyway, so one network hiccup made
    #: the miner burn ten times too little, the backend checked against the amount
    #: and rejected it outright, and **the TAO is not refunded**. This is in the
    #: known-defects table in AGENTS.md §6 (`docs/control_json_example.json` said
    #: 0.01).
    #:
    #: Now: neither control.json nor miner.yaml gave a value → **refuse to burn**,
    #: do not guess. Burning is irreversible, and fail-closed is the only
    #: acceptable default (AGENTS.md §4).
    burn_rate_tao: float | None = None
    limit_price_rao: int = 0

    #: How many blocks are allowed between burn and announce. Going over means the
    #: backend marks it `rejected`, and the TAO is not refunded.
    #:
    #: Where the value comes from: `scanner.burn_block_window` in the production
    #: `backend.yaml`, read at `backend/config.py:77` and enforced at
    #: `scanner/burn_verify.py:71` — **50**. (The docs in this repo said 10 blocks
    #: for a while; on 2026-08-19 they were unified to 50 following "production
    #: behaviour wins".)
    #:
    #: 🟡 **Still a red-line #1 copy, but only until the next protocol release.**
    #: Red line #1 says the burn amount and the block window are always installed
    #: from `openroboto-protocol`, never copied into this repo.
    #:
    #: The protocol side is **done**: `openroboto_protocol.constants.BURN_BLOCK_WINDOW`
    #: exists and carries the same value, the same provenance, and tests pinning all
    #: three properties of the comparison. It is not released yet, and `==0.3.0`
    #: cannot be resolved from PyPI, so importing it here today would simply not
    #: install.
    #:
    #: **When openroboto-protocol 0.3.0 is published, this is a three-line change:**
    #: bump the pin in `pyproject.toml`, `from openroboto_protocol.constants import
    #: BURN_BLOCK_WINDOW`, and make this field default to it. Delete this comment
    #: with it.
    #:
    #: Until then: changing this number requires changing the production
    #: `backend.yaml` and the protocol constant in the same breath. Being stricter
    #: than the backend is not the safe direction — it rejects submissions the
    #: backend would have accepted, and the miner has already burned by then.
    burn_block_window: int = 50

    def require_for_chain(self) -> None:
        """Minimum config check before going on chain.

        One missing item and it refuses outright — on-chain operations cost money,
        so they must not run with a broken config.
        """
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
        """Read one YAML file. The key names match `miner.example.yaml` exactly."""
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
        """Build from an already-parsed YAML mapping.

        Tests go straight through this one, with nothing written to disk.
        """
        cfg = cls()

        subnet = _section(data, "subnet")
        cfg.network = subnet.get("network", cfg.network)
        cfg.subtensor_endpoint = subnet.get(
            "subtensor_endpoint", cfg.subtensor_endpoint
        )
        cfg.netuid = int(subnet.get("netuid", cfg.netuid) or 0)
        cfg.wallet_path = subnet.get("wallet_path", cfg.wallet_path)
        # coldkey / hotkey may be parsed as numbers in YAML (a wallet named `123`),
        # so convert both to strings.
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

        # The payment section in miner.yaml is a local override; normally
        # control.json overrides it.
        payment = _section(data, "payment")
        if payment.get("burn_rate_tao") is not None:
            cfg.burn_rate_tao = float(payment["burn_rate_tao"])
        if payment.get("limit_price_rao") is not None:
            cfg.limit_price_rao = int(payment["limit_price_rao"])

        return cfg

    @classmethod
    def load(cls, path: str) -> Settings:
        """Alias for `from_yaml`; the command layer uses this name throughout."""
        return cls.from_yaml(path)


def _section(data: dict[str, Any], name: str) -> dict[str, Any]:
    """Get a second-level section.

    Returns an empty dict when the section is written empty (`subnet:` with nothing
    after it).
    """
    value = data.get(name)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"配置段 `{name}` 必须是映射，实际是 {type(value).__name__}")
    return value
