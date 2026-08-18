"""把训练产物推到 HuggingFace 公开仓库。

上传的是**目录原样**。评测器只收完整 checkpoint（`params/` 或
`model.safetensors` + `assets/physical-intelligence/libero/norm_stats.json`），
裸 LoRA adapter 会在预检直接被拒 —— 所以 `openroboto check` 应该在上传前跑一遍。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class UploadError(Exception):
    """上传失败。多数情况是基建问题（HF 抖动、token 过期），不是模型有问题。"""


@dataclass(frozen=True)
class UploadResult:
    """一次上传的结果。两个字段都要写进断点，`commit_sha` 最终会上链。"""

    url: str
    commit_sha: str


def commit_sha_from_url(url: str) -> str:
    """从 `.../commit/<sha>` 形式的 URL 里取出 commit SHA，取不到给空串。"""
    if "/commit/" not in url:
        return ""
    return url.split("/commit/", 1)[1][:40]


def push_model(
    model_dir: str,
    repo_id: str,
    hf_token: str,
    round_num: int = 0,
    metrics: dict[str, Any] | None = None,
) -> UploadResult:
    """把 `model_dir` 整个推到 `repo_id`（公开仓库），返回 URL 与 commit SHA。

    旧实现把 `upload_folder` 丢进子线程 + 600 秒超时，超时就当失败返回 `None`。
    π0.5 完整 checkpoint 有好几个 GB，600 秒在正常家用带宽下根本传不完 ——
    那个超时制造的是**假失败**：文件其实还在传，矿工看到 "upload failed" 就重跑。
    这里去掉超时，交给 `huggingface_hub` 自己的分片重试。
    """
    from huggingface_hub import HfApi, create_repo, upload_folder

    path = Path(model_dir)
    if not path.is_dir():
        raise UploadError(
            f"模型目录不存在：{model_dir}\n"
            "  → 先跑 `openroboto train`，或用 --output-dir 指定"
        )

    _write_round_info(path, round_num, metrics)

    total_bytes = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    file_count = sum(1 for f in path.rglob("*") if f.is_file())
    logger.info(
        "📤 准备上传 %s | %d 个文件 %.1f MB",
        repo_id,
        file_count,
        total_bytes / 1024 / 1024,
    )

    try:
        create_repo(repo_id=repo_id, token=hf_token, repo_type="model", exist_ok=True)
        api = HfApi(token=hf_token)
        try:
            api.update_repo_visibility(repo_id, visibility="public", repo_type="model")
        except AttributeError:
            # 老版本 huggingface_hub 没这个方法；create_repo 默认就是公开。
            pass

        commit_url = upload_folder(
            folder_path=str(path),
            repo_id=repo_id,
            repo_type="model",
            token=hf_token,
            commit_message=(
                f"Round {round_num} π₀.₅ LIBERO model — {datetime.now(UTC).isoformat()}"
            ),
        )
        commit_sha = commit_sha_from_url(str(commit_url)) or str(
            api.repo_info(repo_id, repo_type="model").sha
        )
    except Exception as exc:
        raise UploadError(f"推送到 HF 失败：{exc}") from exc

    url = (
        str(commit_url)
        if "/commit/" in str(commit_url)
        else f"https://huggingface.co/{repo_id}"
    )
    logger.info("✅ 已上传 %s | commit=%s", url, commit_sha[:8])
    return UploadResult(url=url, commit_sha=commit_sha)


def _write_round_info(
    path: Path, round_num: int, metrics: dict[str, Any] | None
) -> None:
    """在模型目录里放一份 `round_info.json`，跟着模型一起上传。"""
    info: dict[str, Any] = {
        "model": "pi05",
        "config": "pi05_libero",
        "round_num": round_num,
        "created_at": datetime.now(UTC).isoformat(),
        "client": f"openroboto-cli/{_client_version()}",
    }
    if metrics:
        info["metrics"] = {
            "final_loss": metrics.get("final_loss", 0),
            "action_mse": metrics.get("action_mse", 0),
            "training_steps": metrics.get("training_steps", 0),
            "training_duration_sec": metrics.get("training_duration_seconds", 0),
        }
    (path / "round_info.json").write_text(json.dumps(info, indent=2), encoding="utf-8")


def _client_version() -> str:
    from openroboto import __version__

    return __version__
