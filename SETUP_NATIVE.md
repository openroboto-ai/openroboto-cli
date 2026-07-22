# Native Setup

## Requirements

- Linux with an NVIDIA GPU and recent driver
- Python 3.11
- Docker with NVIDIA Container Toolkit
- Git

## Python environment

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

## Miner configuration

```bash
cp miner.example.yaml miner.yaml
```

Fill the placeholders in `miner.yaml`, then run:

```bash
python miner.py --config miner.yaml
python rt.py submit --config miner.yaml --round 1
```

## Weight-setting validator

```bash
cp validator.example.yaml validator.yaml
python validator.py --config validator.yaml
```

The validator reads public control and weight endpoints and submits weights on chain. Benchmark execution uses the separate public validator toolkit.

## Training container

The host miner launches `openpi-runner/` for OpenPI training. Confirm that Docker can access the GPU before running a training round:

```bash
docker run --rm --gpus all nvidia/cuda:12.4.1-runtime-ubuntu22.04 nvidia-smi
```

Local YAML configuration, state, logs, and model artifacts are ignored by Git.
