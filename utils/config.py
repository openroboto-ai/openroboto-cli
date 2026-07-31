"""
Configuration — Credentials and HTTP URL configuration.
All runtime parameters read from miner.yaml + control.json (HTTP direct link).
NO env fallback — env-only config removed.
"""

from dataclasses import dataclass
from typing import Optional
import yaml

NETWORK_ENDPOINTS = {
    "finney": None,
    "test":   None,
    "local":  "ws://127.0.0.1:9944",
}


@dataclass
class Config:
    """Credential config — stores only immutable connection info."""

    # ─── Bittensor ─────────────────────────────────────
    network: str = "finney"
    subtensor_endpoint: str = ""
    netuid: int = 313
    wallet_path: str = ""
    coldkey: str = "default"
    hotkey: str = "default"
    hotkey_ss58: str = ""
    wallet_password: str = ""  # Wallet unlock password (from miner.yaml)
    weight_interval_min: int = 720

    # ─── HTTP Direct Link URLs ─────────────────────────
    control_json_url: str = ""
    dataset_train_url: str = ""
    dataset_val_url: str = ""
    dataset_test_url: str = ""

    # ─── Model ──────────────────────────────────────────
    model_cache_dir: str = "/models"
    vla_model_id: str = "pi05"
    vla_checkpoint_path: str = ""

    # ─── HuggingFace ──────────────────────────────────
    hf_token: str = ""
    hf_username: str = ""
    hf_merged_model_id: str = ""

    # ─── Log ──────────────────────────────────────────
    log_level: str = "INFO"
    log_dir: str = "logs"

    # ─── Miner ──────────────────────────────────────────
    custom_train_script: str = ""

    # ─── Validator ──────────────────────────────────────
    backend_url: str = "http://localhost:8001"
    backend_public_key: str = ""

    # ─── Payment (from control.json) ──────────────────
    burn_rate_tao: float = 0.01
    limit_price_rao: int = 0

    @property
    def effective_endpoint(self) -> str:
        if self.subtensor_endpoint:
            return self.subtensor_endpoint
        return NETWORK_ENDPOINTS.get(self.network, f"wss://{self.network}.opentensor.ai:443")

    def apply_control(self, control: dict) -> None:
        """Apply owner-managed control.json config to this instance."""
        # Payment config
        payment = control.get("payment", {})
        self.burn_rate_tao = payment.get("burn_rate_tao", self.burn_rate_tao)
        self.limit_price_rao = payment.get("limit_price_rao", self.limit_price_rao)
        # Training config
        training = control.get("training", {})
        if training.get("vla_checkpoint_path"):
            self.vla_checkpoint_path = training["vla_checkpoint_path"]
        if training.get("vla_model_id"):
            self.vla_model_id = training["vla_model_id"]

    @classmethod
    def from_yaml(cls, path: str) -> "Config":
        with open(path, "r") as f:
            data = yaml.safe_load(f) or {}
        cfg = cls()
        subnet = data.get("subnet", {})
        cfg.network = subnet.get("network", cfg.network)
        cfg.subtensor_endpoint = subnet.get("subtensor_endpoint", cfg.subtensor_endpoint)
        cfg.netuid = subnet.get("netuid", cfg.netuid)
        cfg.wallet_path = subnet.get("wallet_path", cfg.wallet_path)
        cfg.coldkey = subnet.get("coldkey", cfg.coldkey)
        cfg.hotkey = subnet.get("hotkey", cfg.hotkey)
        cfg.hotkey_ss58 = subnet.get("hotkey_ss58", cfg.hotkey_ss58)
        cfg.wallet_password = subnet.get("wallet_password", cfg.wallet_password)

        urls = data.get("urls", {})
        cfg.control_json_url = urls.get("control_json", cfg.control_json_url)
        cfg.dataset_train_url = urls.get("dataset_train", cfg.dataset_train_url)
        cfg.dataset_val_url = urls.get("dataset_val", cfg.dataset_val_url)
        cfg.dataset_test_url = urls.get("dataset_test", cfg.dataset_test_url)

        model = data.get("model", {})
        cfg.model_cache_dir = model.get("cache_dir", cfg.model_cache_dir)
        cfg.vla_model_id = model.get("vla_model_id", cfg.vla_model_id)
        cfg.vla_checkpoint_path = model.get("vla_checkpoint_path", cfg.vla_checkpoint_path)

        hf = data.get("huggingface", {})
        cfg.hf_token = hf.get("token", cfg.hf_token)
        cfg.hf_username = hf.get("username", cfg.hf_username)
        cfg.hf_merged_model_id = hf.get("merged_model_id", cfg.hf_merged_model_id)

        cfg.log_level = data.get("log_level", cfg.log_level)
        cfg.log_dir = data.get("log_dir", cfg.log_dir)
        cfg.weight_interval_min = data.get("weight_interval_min", cfg.weight_interval_min)
        cfg.custom_train_script = data.get("custom_train_script", cfg.custom_train_script)

        backend = data.get("backend", {})
        cfg.backend_url = backend.get("url", cfg.backend_url)
        cfg.backend_public_key = backend.get("public_key", cfg.backend_public_key)

        # Payment config (local override in miner.yaml)
        payment = data.get("payment", {})
        if payment.get("burn_rate_tao") is not None:
            cfg.burn_rate_tao = float(payment["burn_rate_tao"])
        if payment.get("limit_price_rao") is not None:
            cfg.limit_price_rao = int(payment["limit_price_rao"])

        return cfg

    @classmethod
    def load(cls, path: Optional[str] = None) -> "Config":
        return cls.from_yaml(path)
