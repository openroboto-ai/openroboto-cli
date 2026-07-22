# openpi-runner — π₀.₅ 训练隔离容器

将 openpi 训练环境隔离到独立 Docker 镜像，与主进程 (bittensor) 完全隔离。

## 为什么需要隔离?

- **openpi** 需要 `numpy<2.0`
- **bittensor==10.5.0** 需要 `numpy>=2.0.1`
- 两者无法共存于同一 Python 环境

## 方案

```
主进程 (bittensor, numpy>=2.0)
  │
  ├─ 下载数据集 (HTTP 直链)
  ├─ 调用 openpi-runner 容器 (docker run -v)
  │    ├─ openpi (numpy<2.0)
  │    ├─ 训练 π₀.₅
  │    └─ 输出 → /data/output
  ├─ 收集 metrics.json + proof.json
  ├─ 推模型到 HF
  └─ 链上公告
```

## 构建镜像

```bash
cd openpi-runner
docker build -t robot-train-openpi .
```

## 手动测试

```bash
docker run --gpus all \
  -v /path/to/data:/data/input \
  -v /path/to/output:/data/output \
  -v ~/.cache/openpi:/data/cache \
  -e CHECKPOINT_PATH=/data/cache/pi05_base \
  -e TRAIN_DATA=/data/input/train.json \
  -e OUTPUT_DIR=/data/output \
  -e EPOCHS=3 \
  -e BATCH_SIZE=4 \
  -e LR=1e-4 \
  robot-train-openpi
```

## 自动调用

主进程通过 `miner/training_pipeline_vla.py` 自动调用：

```python
from miner.training_pipeline_vla import run_training

metrics, policy = run_training(
    train_dataset=episodes,
    output_dir="./output",
    config=config,
)
```

环境变量:
- `OPENPI_RUNNER_IMAGE` — 自定义镜像名 (默认 `robot-train-openpi:latest`)
