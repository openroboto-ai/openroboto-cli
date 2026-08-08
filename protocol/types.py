"""
Shared protocol: data classes, constants, and type definitions.
"""

from dataclasses import dataclass
from typing import List
from datetime import datetime

# ─── Status constants ──────────────────────────────────────
STATUS_WAITING   = "waiting"
STATUS_EVAL      = "evaluating"
STATUS_SCORING   = "scoring"
STATUS_ACTIVE    = "active"
STATUS_PAUSED    = "paused"
STATUS_DONE      = "done"

# ─── π₀.₅ base model constants ─────────────────────────────
DEFAULT_MODEL_ID = "pi05"
DEFAULT_LORA_R = 32
DEFAULT_LORA_ALPHA = 64
DEFAULT_LORA_DROPOUT = 0.1
DEFAULT_LORA_TARGET_MODULES = [
    "q_proj", "k_proj", "v_proj", "o_proj",
    "gate_proj", "up_proj", "down_proj",
]
DEFAULT_BATCH_SIZE = 4
DEFAULT_GRADIENT_ACCUM = 8
DEFAULT_LEARNING_RATE = 1e-4
DEFAULT_EPOCHS = 3
DEFAULT_WARMUP_RATIO = 0.05

# ─── π₀.₅ VLA + LIBERO constants ──────────────────────────────

PI05_BASE_CHECKPOINT = "gs://openpi-assets/checkpoints/pi05_base"
PI05_LIBERO_CONFIG = "pi05_libero"
VLA_ACTION_DIM = 7
VLA_ACTION_CHUNK_SIZE = 50
LIBERO_IMAGE_SIZE = 224
LIBERO_STATE_DIM = 8
LIBERO_TASK_SUITES = [
    "libero_spatial", "libero_object", "libero_goal", "libero_10",
    "libero_object_swap", "libero_spatial_swap",
]

# VLA scoring weights
VLA_SCORING_WEIGHTS = {
    "action_accuracy": 0.40,
    "task_success": 0.30,
    "trajectory_smoothness": 0.15,
    "training_efficiency": 0.15,
}

# ─── Champion / Anti-Sybil constants ─────────────────────

# One-shot commit rule: each hotkey can commit only once
# Subsequent commits are permanently invalid (consistent with Affine)
MAX_COMMIT_COUNT = 1

# Challenge queue window size (blocks)
CHAMPION_WINDOW_BLOCKS = 7200  # ~24h

# Champion challenge: challenger must win on all envs
CHAMPION_MARGIN = 0.01  # Public default; round metadata may publish a different value.
MIN_TASKS_PER_ENV = 5   # Minimum successful samples per env

# Anti-plagiarism: model_hash comparison
# Later submissions with same model_hash marked invalid

# Top-K distribution
TOP_K = 3
TOP_K_EMISSION_WEIGHTS = [0.70, 0.20, 0.10]
DEFAULT_ROUND_DURATION_HOURS = 4

# ─── Data Classes ────────────────────────────────────────────

@dataclass
class TrainingConfig:
    round_num: int
    data_version: int
    model_id: str = DEFAULT_MODEL_ID
    lora_r: int = DEFAULT_LORA_R
    lora_alpha: int = DEFAULT_LORA_ALPHA
    lora_dropout: float = DEFAULT_LORA_DROPOUT
    batch_size: int = DEFAULT_BATCH_SIZE
    gradient_accumulation_steps: int = DEFAULT_GRADIENT_ACCUM
    learning_rate: float = DEFAULT_LEARNING_RATE
    epochs: int = DEFAULT_EPOCHS
    warmup_ratio: float = DEFAULT_WARMUP_RATIO

    def to_dict(self) -> dict:
        """将配置转换为字典。 / Convert config to dictionary."""
        return {
            "round_num": self.round_num,
            "data_version": self.data_version,
            "model_id": self.model_id,
            "lora_r": self.lora_r,
            "lora_alpha": self.lora_alpha,
            "lora_dropout": self.lora_dropout,
            "batch_size": self.batch_size,
            "gradient_accumulation_steps": self.gradient_accumulation_steps,
            "learning_rate": self.learning_rate,
            "epochs": self.epochs,
            "warmup_ratio": self.warmup_ratio,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "TrainingConfig":
        """从字典创建配置实例。 / Create config instance from dictionary."""
        return cls(**d)


# ═══════════════════════════════════════════════════════════
# NEW: HF Model Submission (Miner → Chain)
# ═══════════════════════════════════════════════════════════

@dataclass
class HFModelSubmission:
    """
    After miner training completes, push model to HF public repo,
    and announce submission via on-chain Commitment.
    """
    miner_hotkey: str
    round_num: int
    hf_repo_id: str                # e.g. "username/pi05-libero-round-1"
    hf_repo_url: str               # e.g. "https://huggingface.co/username/pi05-libero-round-1"
    training_loss: float
    training_steps: int
    training_duration_sec: float
    gpu_device: str = ""
    submitted_at: str = ""

    def to_dict(self) -> dict:
        """将提交信息转换为字典。 / Convert submission to dictionary."""
        return {
            "miner_hotkey": self.miner_hotkey,
            "round_num": self.round_num,
            "hf_repo_id": self.hf_repo_id,
            "hf_repo_url": self.hf_repo_url,
            "training_loss": self.training_loss,
            "training_steps": self.training_steps,
            "training_duration_sec": round(self.training_duration_sec, 1),
            "gpu_device": self.gpu_device,
            "submitted_at": self.submitted_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "HFModelSubmission":
        """从字典创建提交实例。 / Create submission instance from dictionary."""
        return cls(**d)


# ═══════════════════════════════════════════════════════════
# NEW: Validator Benchmark Score (Validator → Chain)
# ═══════════════════════════════════════════════════════════

@dataclass
class BenchmarkScore:
    """
    Validator score results after running benchmark on miner model,
    Submitted via on-chain Commitment announcement.
    """
    miner_hotkey: str
    round_num: int
    hf_repo_id: str
    validator_hotkey: str

    # LIBERO benchmark scores (0-1)
    action_accuracy: float       # 0-1
    task_success: float          # 0-1
    trajectory_smoothness: float # 0-1
    efficiency: float            # 0-1
    total_score: float

    # Extra info
    benchmark_duration_sec: float = 0.0
    docker_image_used: str = ""  # Unified base Docker image
    scored_at: str = ""

    def to_dict(self) -> dict:
        """将分数记录转换为字典。 / Convert score record to dictionary."""
        return {
            "miner_hotkey": self.miner_hotkey,
            "round_num": self.round_num,
            "hf_repo_id": self.hf_repo_id,
            "validator_hotkey": self.validator_hotkey,
            "action_accuracy": round(self.action_accuracy, 4),
            "task_success": round(self.task_success, 4),
            "trajectory_smoothness": round(self.trajectory_smoothness, 4),
            "efficiency": round(self.efficiency, 4),
            "total_score": round(self.total_score, 4),
            "benchmark_duration_sec": round(self.benchmark_duration_sec, 1),
            "docker_image_used": self.docker_image_used,
            "scored_at": self.scored_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "BenchmarkScore":
        """从字典创建分数实例。 / Create score instance from dictionary."""
        return cls(**d)

    @staticmethod
    def compute_total(action_accuracy, task_success, trajectory_smoothness, efficiency):
        """计算加权总分。 / Compute weighted total score using VLA_SCORING_WEIGHTS."""
        w = VLA_SCORING_WEIGHTS
        return (w["action_accuracy"] * action_accuracy
                + w["task_success"] * task_success
                + w["trajectory_smoothness"] * trajectory_smoothness
                + w["training_efficiency"] * efficiency)


# ═══════════════════════════════════════════════════════════
# VLA Episode (LeRobot compatible)
# ═══════════════════════════════════════════════════════════

EPISODE_REQUIRED_FIELDS = [
    "episode_id", "timestamp", "observation", "action",
    "language_instruction", "license",
]
ALLOWED_LICENSES = ["CC-BY-4.0", "CC-BY-SA-4.0", "CC0-1.0", "Apache-2.0", "MIT", "OpenRail"]


@dataclass
class VLAEpisode:
    episode_id: str
    timestamp: str
    observation: dict
    action: list
    language_instruction: str
    license: str = "CC-BY-4.0"

    def validate(self) -> List[str]:
        """验证 episode 字段完整性。 / Validate episode field completeness."""
        errors = []
        for f in EPISODE_REQUIRED_FIELDS:
            if not hasattr(self, f) or getattr(self, f) is None:
                errors.append(f"Missing: {f}")
        if self.license not in ALLOWED_LICENSES:
            errors.append(f"Invalid license: {self.license}")
        return errors

    def to_dict(self) -> dict:
        """将 episode 转换为字典。 / Convert episode to dictionary."""
        return {
            "episode_id": self.episode_id,
            "timestamp": self.timestamp,
            "observation": self.observation,
            "action": self.action,
            "language_instruction": self.language_instruction,
            "license": self.license,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "VLAEpisode":
        """从字典创建 episode 实例。 / Create episode instance from dictionary."""
        import inspect
        sig = inspect.signature(cls.__init__)
        params = set(sig.parameters.keys()) - {"self"}
        clean = {k: v for k, v in d.items() if k in params}
        return cls(**clean)


@dataclass
class EpisodeBatch:
    batch_id: str
    episodes: List[VLAEpisode]
    pushed_at: str = ""
    pushed_by: str = ""
    s3_key: str = ""

    def to_dict(self) -> dict:
        """将批次转换为字典。 / Convert batch to dictionary."""
        return {
            "batch_id": self.batch_id,
            "episodes": [ep.to_dict() for ep in self.episodes],
            "pushed_at": self.pushed_at or datetime.now().isoformat(),
            "pushed_by": self.pushed_by,
            "s3_key": self.s3_key,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "EpisodeBatch":
        """从字典创建批次实例。 / Create batch instance from dictionary."""
        return cls(
            batch_id=d["batch_id"],
            episodes=[VLAEpisode.from_dict(ep) for ep in d.get("episodes", [])],
            pushed_at=d.get("pushed_at", ""),
            pushed_by=d.get("pushed_by", ""),
            s3_key=d.get("s3_key", ""),
        )
