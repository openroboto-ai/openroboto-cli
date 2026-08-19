# Miner Deployment Guide

> **Status**: current · **Updated**: 2026-08-19 · **Audience**: miners running on their own hardware
> **Scope**: Machine preparation, install, training image, running a round, systemd.
> **Note**: The conceptual guide is [MINER.md](./MINER.md). This document assumes you have read it.

> For miners running on Ubuntu 22.04/24.04 with NVIDIA GPU.

## Hardware Requirements

| Component | Minimum | Recommended |
|-----------|---------|-------------|
| GPU | 1× NVIDIA GPU, 24 GB VRAM | 1× A100 80GB |
| RAM | 32 GB | 64 GB |
| Disk | 200 GB SSD | 500 GB NVMe |
| OS | Ubuntu 22.04 / 24.04 | Ubuntu 24.04 |

## 1. Prerequisites

```bash
# NVIDIA driver (skip if already installed)
nvidia-smi

# Docker + NVIDIA Container Toolkit
curl -fsSL https://get.docker.com | sh
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
sudo apt-get update
sudo apt-get install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker

# Verify GPU Docker
docker run --rm --gpus all nvidia/cuda:12.1.0-runtime-ubuntu22.04 nvidia-smi
```

## 2. Install

**No repository clone.** The CLI ships as a package on PyPI; `openroboto init`
writes out the config and training-strategy files you need.

```bash
# Create deploy user
sudo useradd -m -s /bin/bash robot-train
sudo -i -u robot-train

# Python 3.11 venv + install
python3.11 -m venv .venv
source .venv/bin/activate
pip install openroboto

openroboto --version    # CLI version + protocol package version
```

The π0.5 base checkpoint needs no manual download step. Leave
`model.vla_checkpoint_path` unset and the training container fetches it into
`cache/pi05_base` on the first run; later rounds hit that cache.

## 3. Configuration

```bash
openroboto init my-miner    # writes miner.yaml + a training strategy script
cd my-miner
nano miner.yaml
```

### `miner.yaml` (required fields)

```yaml
subnet:
  network: finney            # or "test" for testnet
  netuid: 80
  coldkey: <your-coldkey>
  hotkey: <your-hotkey>
  hotkey_ss58: 5MinerCexampleexampleexampleexampleeCCCCCCCCCCCC  # Full SS58

urls:
  control_json: https://<host>/metadata/control.json

huggingface:
  token: hf_xxx
  username: <your-hf-username>

log_level: INFO
```

> **The nested layout above is the only one that parses.** Every key lives under
> a section (`subnet:`, `urls:`, `huggingface:`, …). Older guides showed a flat
> `[DEFAULT]` / `key = value` form — that no longer loads, and it fails *quietly*:
> the file parses, every field falls back to its default, and you find out when a
> command complains about a missing `netuid`. Run `openroboto doctor` after
> editing; it names every field that is missing or unusable.

Payment fields are deliberately absent: `burn_rate_tao` and `limit_price_rao`
come from the subnet's `control.json`, never from `miner.yaml`. See §"Notes".

## 4. Build OpenPi Runner Docker Image

```bash
openroboto build
```

Training runs in Docker because openpi needs `numpy<2.0` while bittensor needs
`numpy>=2.0` — one interpreter cannot hold both.

`build` uses `./openpi-runner` as the build context when it exists and otherwise
pulls the public one, so there is nothing to clone. Override the image name with
`--image` or `$OPENPI_RUNNER_IMAGE`.

> **One universal image** — no need to rebuild for custom training strategies.
> Your strategy script is mounted in as a volume (see below).

## 5. Custom Training Strategy (Optional)

`openroboto init` already dropped a working `train_strategy.py` next to your
`miner.yaml`. Edit that file, or point at a different one.

### Default: use the generated strategy

```bash
openroboto train                     # uses ./train_strategy.py
openroboto init -s example           # swap in the teaching version instead
```

### Custom: your own training script

Either pass it per-run or record it in `miner.yaml`:

```bash
openroboto train -s /path/to/my_strategy.py
```

```yaml
# miner.yaml — used when -s is not given
custom_train_script: "/path/to/my_strategy.py"
```

The script is verified to exist, mounted into the container, and used as the
training entrypoint instead of the default.

### Strategy script interface

Your script must define a `train()` function:

```python
def train(cfg: dict, episodes: list, policy=None) -> tuple:
    """
    Custom training strategy.

    Args:
        cfg: config dict (checkpoint_path, epochs, batch_size, lr, lora_r, lora_alpha, hotkey, output_dir)
        episodes: training data list
        policy: openpi policy object (with checkpoint loaded)

    Returns:
        (metrics_dict, proof_dict)
        metrics must include: final_loss, training_steps
    """
    # ... your training logic ...
    return metrics, proof
```

### Output requirements

Your strategy must produce:
```
/data/output/
├── adapter/                    # LoRA adapter weights
│   ├── adapter_config.json     # LoRA metadata (r, alpha, target_modules)
│   └── adapter_model.safetensors # LoRA weight matrices
├── metrics.json                # Training metrics (final_loss, training_steps, ...)
└── proof.json                  # Training proof (GPU, timestamps, ...)
```

### Example

`openroboto init` writes a complete working example to `train_strategy.py`, and
`openroboto init -s example` writes a more heavily commented teaching version. Both
produce valid adapter files. There is nothing to clone to read them.

## 6. Run Miner

The workflow is split into two stages:

### Check the environment first

```bash
openroboto doctor
```

`doctor` exists to make "burned TAO, then discovered the environment was wrong"
impossible. It checks GPU, Docker, the NVIDIA toolkit, HF permissions, wallet
balance, `control.json` reachability and every required config field — **before**
anything costs money.

### Train

```bash
openroboto train        # one round, then exits
```

After training completes, state is saved to `state/round_N.json`.

### Validate the model locally — before paying

```bash
openroboto check
```

Same format rules the evaluator applies. This used to require cloning a second
repository; it is now built in.

> **⚠️ Training produces a LoRA adapter, and a bare adapter is rejected.** A
> merged full checkpoint is what gets evaluated. There is **no `openroboto merge`
> command yet** — until there is, merging is a manual step, so run
> `openroboto check` before you burn anything.

### Submit: Upload → Burn → Announce

```bash
openroboto submit       # full pipeline, resumable

# Individual steps (recovery/debugging only — see warning below):
openroboto upload --round 1
openroboto burn
openroboto announce --round 1
```

> **⚠️ Do not split burn and announce.** The backend enforces a burn→commitment
> window of **50 blocks (~10 minutes)**; exceeding it rejects the submission and
> the burned TAO is not refunded. This prevents burn replay (paying once,
> submitting later or repeatedly). Use one-shot `openroboto submit` — the
> individual-step commands are for recovery and debugging only. `announce`
> refuses to submit once the window has passed, rather than charging you a
> commitment fee for a submission that is already doomed.

`submit` is resumable: re-running it skips steps already recorded in
`state/round_N.json` and **reuses an existing burn instead of paying twice**.

### Check what the backend made of it

```bash
openroboto status       # submission state + rejection reason, if any
```

## 7. Verify On-Chain Submission

Check the backend API for your submission status:

```bash
# Check backend API
curl http://localhost:8001/api/miner/<your-hotkey-short>

# Check backend scanner logs
tail -f backend/data/backend.log
```

## 8. systemd Service (Optional)

```bash
sudo tee /etc/systemd/system/robot-train-miner.service << 'EOF'
[Unit]
Description=RobotTrain Miner
After=network.target docker.service

[Service]
Type=oneshot
User=robot-train
WorkingDirectory=/home/robot-train/my-miner
ExecStart=/home/robot-train/.venv/bin/openroboto train --config miner.yaml
Environment=PATH=/home/robot-train/.venv/bin:/usr/bin:/bin
Environment=HF_TOKEN=<your-token>

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable robot-train-miner.service
sudo systemctl start robot-train-miner.service

# Monitor
tail -f /home/robot-train/my-miner/logs/*.log
```

> This unit only **trains**. It deliberately does not run `submit`: that step
> burns TAO, and an unattended service that pays money on a timer is how a
> misconfigured round turns into a string of wasted burns. Run `openroboto submit`
> yourself after checking `openroboto check`.

## Troubleshooting

### Docker GPU not available
```bash
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
docker run --rm --gpus all nvidia/cuda:12.1.0-runtime-ubuntu22.04 nvidia-smi
```

### Commitment submitted but backend doesn't see it
```bash
# Check backend API
curl http://localhost:8001/api/miner/<your-hotkey-short>

# Check backend scanner logs
tail -f backend/data/backend.log

# Check for scan rejections (if burn verification failed)
curl "http://localhost:8001/api/v1/scan-rejections?hotkey=<your-hotkey-ss58>"
```

### Training container fails
```bash
# Check openpi-runner image
docker images | grep robot-train-openpi

# Run manually for debugging
docker run --rm --gpus all -v /data:/data robot-train-openpi:latest nvidia-smi
```

## Important Notes

- `openroboto train` runs **once per round** then exits. No polling loop.
- `openroboto submit` runs the post-training pipeline (upload → burn → announce).
- State is saved to `state/round_N.json` — re-running `openroboto submit` resumes from the last completed step and reuses an existing burn instead of paying twice.
- Backend scanner picks up submissions within ~60 seconds.
- Payment config (`burn_rate_tao`, `limit_price_rao`) comes from the owner's `control.json`, not `miner.yaml`. If `control.json` cannot be fetched, `burn` **refuses to run** rather than guessing an amount — a wrong amount is rejected by the backend and the TAO is not refunded.
- Backend verifies burn tx using **strict exact match** (no `startswith` prefix matching).
- Anti-plagiarism: backend computes LFS fingerprint (`repo_hash`) for each submission; same hash from different hotkey → rejected.
- Seed computation failure is auto-retried (`seed_failed` status), no manual intervention needed.
- If your submission fails burn verification, check `/api/v1/scan-rejections` for the exact rejection reason.