"""每一轮的断点文件 `state/round_N.json`。

train → upload → burn → announce 是四条**分别可能失败**的命令，中间状态必须落盘：
训练跑了六个小时，upload 断在网络上，重跑不能从头训。文件格式与旧
`miner.py` / `rt.py` 写的完全一致 —— 正在跑的矿工升级 CLI 之后，
手上那份 `state/round_1.json` 要能被直接读下去。

唯一的变化是**目录位置**：旧代码把 state 放在 `<仓库目录>/state`
（`os.path.dirname(__file__)`）。装成 pip 包之后那个位置在 site-packages 里，
所以改成相对**当前工作目录**的 `./state`。矿工原本就是在仓库目录里敲命令，
路径实际没变。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

STATE_DIR = Path("state")
"""断点目录，相对当前工作目录。"""

DEFAULT_OUTPUT_ROOT = Path("./tmp/robot_train_vla_miner")
"""训练输出根目录。名字沿用旧默认值 —— 矿工的脚本和 systemd unit 里写着它。"""


class StateError(Exception):
    """断点文件缺失或无法判断轮次。消息里必须给出下一步怎么办。"""


def state_path(round_num: int, base: Path = STATE_DIR) -> Path:
    return base / f"round_{round_num}.json"


def load_state(round_num: int, base: Path = STATE_DIR) -> dict[str, Any]:
    """读一轮的断点。文件不存在或读坏了都当空 —— 空状态会让上游命令从头做。"""
    path = state_path(round_num, base)
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def save_state(round_num: int, state: dict[str, Any], base: Path = STATE_DIR) -> None:
    """写一轮的断点。目录不存在就建。"""
    base.mkdir(parents=True, exist_ok=True)
    state_path(round_num, base).write_text(
        json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def is_step_done(state: dict[str, Any], step: str) -> bool:
    """某一步是否已经跑完。`step` + `status` 两个字段一起判，缺一不可。"""
    return state.get("step") == step and state.get("status") == "completed"


def resolve_round(explicit: int, base: Path = STATE_DIR) -> int:
    """定位要操作哪一轮：显式 `--round` 优先，否则取最新一个跑完的轮次。

    Raises:
        StateError: 一个完成的断点都没有 —— 此时猜轮次等于猜矿工要花的钱，宁可停。
    """
    if explicit and explicit > 0:
        return explicit

    candidates: list[int] = []
    if base.is_dir():
        for entry in base.glob("round_*.json"):
            try:
                candidates.append(int(entry.stem.split("_", 1)[1]))
            except (IndexError, ValueError):
                continue

    for round_num in sorted(candidates, reverse=True):
        if load_state(round_num, base).get("status") == "completed":
            return round_num

    raise StateError(
        "无法自动判断轮次：state/ 下没有已完成的断点。\n"
        "  → 用 `--round N` 显式指定，或先跑 `openroboto train`"
    )


def resolve_output_dir(round_num: int, base: Path = STATE_DIR) -> str:
    """这一轮的模型输出目录。断点里记了就用记的，否则按默认规则拼。"""
    recorded = load_state(round_num, base).get("round_output")
    if isinstance(recorded, str) and recorded:
        return recorded
    return str(DEFAULT_OUTPUT_ROOT / f"round_{round_num}")


def training_metrics(round_num: int, base: Path = STATE_DIR) -> dict[str, Any]:
    """这一轮训练出来的指标，会随模型一起传到 HF。"""
    metrics = load_state(round_num, base).get("training_metrics")
    return metrics if isinstance(metrics, dict) else {}
