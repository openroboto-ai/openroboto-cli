"""`openroboto upload` —— 把训练产物推到 HuggingFace（旧 `rt.py upload`）。"""

from __future__ import annotations

import argparse
from typing import Any

from openroboto.config import ConfigError, Settings
from openroboto.console import say
from openroboto.huggingface import build_repo_id, push_model
from openroboto.round_state import (
    load_state,
    resolve_output_dir,
    resolve_round,
    save_state,
    training_metrics,
)


def add_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("upload", help="把模型传到 HuggingFace")
    parser.add_argument("--config", default="miner.yaml")
    parser.add_argument("--round", type=int, default=0)
    parser.add_argument("--output-dir", default="")
    parser.set_defaults(handler=run)


def run(args: argparse.Namespace) -> int:
    settings = Settings.load(args.config)
    round_num = resolve_round(args.round)
    output_dir = args.output_dir or resolve_output_dir(round_num)

    state = load_state(round_num)
    perform_upload(settings, round_num, output_dir, state)
    say(f"✅ 已上传：{state['hf_url']}")
    say(f"   commit={state['hf_commit'][:8]} repo={state['hf_repo_id']}")
    say("   → 下一步 `openroboto burn`（会先跑一遍花钱前的自检）")
    return 0


def perform_upload(
    settings: Settings,
    round_num: int,
    output_dir: str,
    state: dict[str, Any],
    reuse_existing: bool = False,
) -> None:
    """上传并把结果写进断点。

    `reuse_existing=True` 时，断点里已经有上传结果就直接复用 —— `submit` 走这条，
    避免一条流水线里把几个 GB 重传一遍。单独敲 `openroboto upload` **一定重传**：
    矿工重新导出了 checkpoint 再传，这时候「跳过」是错的答案。
    """
    uploaded = all(state.get(key) for key in ("hf_repo_id", "hf_url", "hf_commit"))
    if reuse_existing and uploaded:
        say(f"⏭️  已上传过：{state['hf_url']}")
        return

    if not settings.hf_token:
        raise ConfigError(
            "未配置 huggingface.token —— 上传需要一个有写权限的 HF token\n"
            "  → https://huggingface.co/settings/tokens 生成后填进 miner.yaml"
        )

    hotkey_ss58 = state.get("hotkey_ss58") or settings.hotkey_ss58
    repo_id = build_repo_id(settings, str(hotkey_ss58 or ""))
    result = push_model(
        model_dir=output_dir,
        repo_id=repo_id,
        hf_token=settings.hf_token,
        round_num=round_num,
        metrics=training_metrics(round_num),
    )

    state["hf_repo_id"] = repo_id
    state["hf_url"] = result.url
    state["hf_commit"] = result.commit_sha
    state["step"] = "upload"
    state["status"] = "completed"
    if hotkey_ss58:
        state["hotkey_ss58"] = hotkey_ss58
    save_state(round_num, state)
