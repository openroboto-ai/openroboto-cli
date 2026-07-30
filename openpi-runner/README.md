# openpi-runner — Isolated π₀.₅ Training Container

Isolates the openpi training environment in a dedicated Docker image, fully separated from the main process (bittensor).

## Why isolation?

- **openpi** requires `numpy<2.0`
- **bittensor==10.5.0** requires `numpy>=2.0.1`
- The two cannot coexist in one Python environment

## Design

```
main process (bittensor, numpy>=2.0)
  │
  ├─ download dataset (direct HTTP)
  ├─ invoke openpi-runner container (docker run -v)
  │    ├─ openpi (numpy<2.0)
  │    ├─ train π₀.₅
  │    └─ output → /data/output
  ├─ collect metrics.json + proof.json
  ├─ push model to HF
  └─ announce on chain
```

## Build the image

```bash
cd openpi-runner
docker build -t robot-train-openpi .
```

## Manual test

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

## Automatic invocation

The main process calls it through `miner/training_pipeline_vla.py`:

```python
from miner.training_pipeline_vla import run_training

metrics, policy = run_training(
    train_dataset=episodes,
    output_dir="./output",
    config=config,
)
```

Environment variables:
- `OPENPI_RUNNER_IMAGE` — custom image name (default `robot-train-openpi:latest`)
