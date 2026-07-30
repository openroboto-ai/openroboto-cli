# Miner Deployment Guide

## Requirements

- Linux with an NVIDIA GPU and recent driver
- Python 3.11
- Docker with NVIDIA Container Toolkit
- Bittensor mainnet wallet and registered hotkey
- Hugging Face account with a write token

Confirm GPU access:

```bash
nvidia-smi
docker run --rm --gpus all nvidia/cuda:12.4.1-runtime-ubuntu22.04 nvidia-smi
```

## Install

```bash
git clone https://github.com/<your-org>/<public-repository>.git
cd <public-repository>
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
bash download_checkpoint.sh
docker build -t openroboto-openpi-runner:latest openpi-runner/
```

## Configure

```bash
cp miner.example.yaml miner.yaml
```

Fill only local values in `miner.yaml`. Use explicit placeholders until the file is on the target host. The file is ignored by Git.

Required areas:

- Bittensor network, netuid, local wallet names, and hotkey address;
- public `control.json` URL;
- public training-resource URLs if they are not supplied by `control.json`;
- local checkpoint and cache paths;
- Hugging Face username and write token.

## Run

```bash
python miner.py --config miner.yaml
python rt.py submit --config miner.yaml --round 1
```

For manual recovery, run the submission phases separately:

```bash
python rt.py upload --config miner.yaml --round 1
python rt.py burn --config miner.yaml --round 1
python rt.py announce --config miner.yaml --round 1
```

Review the saved state before retrying a burn or announcement. Do not repeat an irreversible transaction without confirming its chain result.

## Verify

Verify the following public evidence:

- the Hugging Face repository resolves to the announced immutable commit;
- the burn transaction exists and matches the public round fee;
- the chain commitment contains the intended hotkey, round, repository, commit, and payment reference;
- the published seed can be recomputed from the commitment block hash and recorded drand value.

## Service operation

For long-running operation, wrap `miner.py` in the process manager used by the host. Store configuration outside the repository, set restrictive file permissions, and keep logs in the ignored `logs/` directory.

## Troubleshooting

- GPU unavailable: verify the host driver, Docker runtime, and `--gpus all` test.
- Training container fails: run the `openpi-runner` image interactively and inspect the mounted paths.
- Upload fails: confirm the repository name, token permissions, free disk space, and network access before continuing.
- Commitment not visible: query the public Bittensor commitment for the submitting hotkey and confirm the network and netuid.

