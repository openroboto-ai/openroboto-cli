# 📝 自定义训练策略 — 使用指南

## 概览

自定义训练脚本通过 **volume 挂载** 注入容器，无需重新构建 Docker 镜像即可替换训练逻辑。

```
主机 (miner)                          容器 (openpi-runner)
┌─────────────────┐              ┌─────────────────────────┐
│                 │  -v mount    │                         │
│ my_strategy.py  │ ─────────►  │ /data/scripts/my_       │
│                 │              │   strategy.py           │
│                 │  -e env      │                         │
│                 │ ─────────►  │ CUSTOM_TRAIN=...        │
│                 │              │                         │
│ train_vla()     │  docker run  │ train_runner.py         │
│   ├─ custom_    │ ─────────►  │   ├─ 检测 CUSTOM_TRAIN  │
│   │  train_     │              │   ├─ 调用 _run_custom() │
│   │  script=... │              │   │   └─ train(cfg,…)   │
│                 │              │   └─ 写 metrics.json    │
│                 │ ◄──────────  │     proof.json          │
│ 读回结果 ←─── stdout + 文件    │                         │
└─────────────────┘              └─────────────────────────┘
```

## 脚本接口

你的脚本必须包含一个 `train` 函数：

```python
def train(cfg: dict, episodes: list, policy) -> tuple:
    """
    Args:
        cfg: 配置字典，包含以下键:
            - checkpoint_path: 基础模型路径 (可能已被解析为本地路径)
            - train_data: 训练数据路径
            - val_data: 验证数据路径 (可选)
            - output_dir: 输出目录 (已挂载)
            - epochs: 训练轮数
            - batch_size: 批次大小
            - learning_rate: 学习率
            - warmup_ratio: warmup 比例
            - lora_r: LoRA rank
            - lora_alpha: LoRA alpha
            - hotkey: miner hotkey
        episodes: 已加载的训练数据列表 (list[dict])
        policy: openpi policy 对象，已加载好 π₀.₅ checkpoint

    Returns:
        (metrics, proof) 两个字典:
        - metrics: 包含 final_loss, training_steps, loss_curve 等
        - proof: 包含 miner_uid, gpu_device, started_at, ended_at 等
    """
    ...
```

## 快速示例

### 示例 1: 最小可运行脚本

```python
import time
from datetime import datetime, timezone

def train(cfg, episodes, policy):
    """最小可运行训练脚本 — 遍历所有 episode 计算 dummy loss。"""

    start = time.time()
    steps = 0
    loss_curve = []

    for epoch in range(cfg["epochs"]):
        for ep in episodes:
            steps += 1
            # ← 这里替换成你的训练逻辑
            loss = 1.0 / (1 + steps * 0.01)
            if steps % 10 == 0:
                loss_curve.append({"step": steps, "loss": round(loss, 6)})

    # 保存模型
    adapter_dir = f"{cfg['output_dir']}/adapter"
    if hasattr(policy, "save_pretrained"):
        policy.save_pretrained(adapter_dir)

    metrics = {
        "final_loss": loss_curve[-1]["loss"] if loss_curve else 0.5,
        "training_steps": steps,
        "training_duration_seconds": time.time() - start,
        "loss_curve": loss_curve,
    }

    proof = {
        "miner_uid": cfg["hotkey"],
        "started_at": datetime.now(timezone.utc).isoformat(),
        "ended_at": datetime.now(timezone.utc).isoformat(),
        "total_steps": steps,
        "gpu_device": _gpu_name(),
    }

    return metrics, proof


def _gpu_name():
    try:
        import torch
        if torch.cuda.is_available():
            return torch.cuda.get_device_name(0)
    except ImportError:
        pass
    return "cpu"
```

### 示例 2: 使用 openpi 原生训练 API

```python
import time
import torch
from datetime import datetime, timezone
from openpi.training import data_loader as _data_loader

def train(cfg, episodes, policy):
    """使用 openpi 原生 data loader 和训练循环。"""

    start = time.time()
    optimizer = torch.optim.Adam(policy.parameters(), lr=cfg["learning_rate"])
    steps = 0
    loss_curve = []

    for epoch in range(cfg["epochs"]):
        loader = _data_loader.create_dataloader(
            episodes,
            batch_size=cfg["batch_size"],
        )
        for batch in loader:
            optimizer.zero_grad()
            outputs = policy(batch)
            loss = outputs.loss
            loss.backward()
            optimizer.step()
            steps += 1

            if steps % 10 == 0:
                loss_curve.append({
                    "step": steps,
                    "loss": round(loss.item(), 6),
                })

    # 保存 adapter
    adapter_dir = f"{cfg['output_dir']}/adapter"
    policy.save_pretrained(adapter_dir)

    metrics = {
        "final_loss": loss_curve[-1]["loss"] if loss_curve else 0.5,
        "training_steps": steps,
        "training_duration_seconds": time.time() - start,
        "loss_curve": loss_curve,
    }

    proof = {
        "miner_uid": cfg["hotkey"],
        "started_at": datetime.now(timezone.utc).isoformat(),
        "ended_at": datetime.now(timezone.utc).isoformat(),
        "total_steps": steps,
        "gpu_device": _gpu_name(),
    }

    return metrics, proof


def _gpu_name():
    try:
        import torch
        if torch.cuda.is_available():
            return torch.cuda.get_device_name(0)
    except ImportError:
        pass
    return "cpu"
```

### 示例 3: 自定义优化策略

```python
import time
import torch
from datetime import datetime, timezone

def train(cfg, episodes, policy):
    """示例: 自定义 scheduler + gradient clipping。"""

    start = time.time()

    optimizer = torch.optim.AdamW(
        policy.parameters(),
        lr=cfg["learning_rate"],
        weight_decay=1e-2,
    )

    total_steps = cfg["epochs"] * len(episodes)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(
        optimizer, T_max=total_steps,
    )

    steps = 0
    loss_curve = []
    max_grad_norm = cfg.get("max_grad_norm", 1.0)

    for epoch in range(cfg["epochs"]):
        for ep in episodes:
            # ← 你的 forward pass
            loss = compute_loss(policy, ep)

            loss.backward()
            torch.nn.utils.clip_grad_norm_(policy.parameters(), max_grad_norm)
            optimizer.step()
            optimizer.zero_grad()

            scheduler.step()
            steps += 1

            if steps % 10 == 0:
                loss_curve.append({
                    "step": steps,
                    "loss": round(loss.item(), 6),
                    "lr": scheduler.get_last_lr()[0],
                })

    # 保存
    adapter_dir = f"{cfg['output_dir']}/adapter"
    policy.save_pretrained(adapter_dir)

    metrics = {
        "final_loss": loss_curve[-1]["loss"] if loss_curve else 0.5,
        "training_steps": steps,
        "training_duration_seconds": time.time() - start,
        "loss_curve": loss_curve,
    }

    proof = {
        "miner_uid": cfg["hotkey"],
        "started_at": datetime.now(timezone.utc).isoformat(),
        "ended_at": datetime.now(timezone.utc).isoformat(),
        "total_steps": steps,
        "gpu_device": _gpu_name(),
    }

    return metrics, proof


def compute_loss(policy, episode):
    """← 你的损失函数逻辑。"""
    return ...


def _gpu_name():
    try:
        import torch
        if torch.cuda.is_available():
            return torch.cuda.get_device_name(0)
    except ImportError:
        pass
    return "cpu"
```

## 使用方式

### 方法 1: 在 miner.py 中传入

```python
from miner.trainer_vla import train_vla

train_vla(
    checkpoint_path=...,
    train_json_path=...,
    output_dir="/tmp/output_vla",
    config=train_cfg,
    hf_token=hf_token,
    custom_train_script="/path/to/my_strategy.py",  # ← 加这一行
)
```

### 方法 2: 通过 config.yaml

在 miner 配置中添加路径：

```yaml
training:
  custom_train_script: /path/to/my_strategy.py
```

然后在 `trainer_vla.py` 或 `training_pipeline_vla.py` 中读取配置传入。

### 方法 3: 直接 docker run 测试

跳过 miner，直接测试脚本：

```bash
docker run --rm --gpus all \
  -v /host/data:/data/input \
  -v /host/output:/data/output \
  -v /path/to/my_strategy.py:/data/scripts/my_strategy.py \
  -e CHECKPOINT_PATH=/data/cache/pi05_base \
  -e TRAIN_DATA=/data/input/train.json \
  -e OUTPUT_DIR=/data/output \
  -e EPOCHS=3 \
  -e BATCH_SIZE=4 \
  -e LR=1e-4 \
  -e CUSTOM_TRAIN=/data/scripts/my_strategy.py \
  robot-train-openpi
```

## 目录结构

```
my_training/
├── my_strategy.py          # 自定义训练脚本
├── utils.py                # 自定义模块 (可选)
└── configs/
    └── my_config.yaml      # 自定义配置 (可选)

# 挂载整个目录:
docker run -v /path/to/my_training:/data/scripts ...
# 然后在脚本里引用相对路径
```

## 注意事项

1. **必须提供 `train(cfg, episodes, policy)` 函数**，否则容器会报错退出
2. **必须返回 `(metrics, proof)` 两个字典**，格式需与默认流程一致
3. **模型保存目录** 必须是 `cfg['output_dir']/adapter`，否则 validator 找不到模型
4. **openpi 模块可用** — 容器已安装 openpi，脚本中可直接 `import openpi.*`
5. **GPU 可用** — torch 和 CUDA 在容器内正常工作
6. **临时目录** — `/tmp` 容器内可用，但容器退出后数据丢失；持久化输出必须写到 `cfg['output_dir']`

## 可用 import

容器内预装了以下常用库：

| 类别 | 可用库 |
|---|---|
| 深度学习 | `torch`, `torch.nn`, `torch.optim` |
| openpi | `openpi.shared.download`, `openpi.training.config`, `openpi.policies.policy_config`, `openpi.training.data_loader` |
| 数据处理 | `numpy`, `json`, `pickle` |
| 系统 | `os`, `time`, `datetime`, `logging`, `importlib` |
