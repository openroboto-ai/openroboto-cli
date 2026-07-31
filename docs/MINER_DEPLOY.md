# Miner Deployment Guide

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

## 2. Clone & Setup

```bash
# Create deploy user
sudo useradd -m -s /bin/bash robot-train
sudo -i -u robot-train

# Clone repo
git clone https://github.com/<your-org>/robot-train-subnet.git
cd robot-train-subnet

# Python venv
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Download π0.5 checkpoint
bash download_checkpoint.sh
# Or: bash download_checkpoint.sh --force  (re-download)
```

## 3. Configuration

```bash
cp miner.example.yaml miner.yaml
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

model:
  vla_checkpoint_path: /path/to/cache/pi05_base  # From download_checkpoint.sh

log_level: INFO
```

### `miner.yaml` (alternative, flat format)

```ini
[DEFAULT]
hotkey_ss58 = 5MinerCexampleexampleexampleexampleeCCCCCCCCCCCC
hf_token = hf_xxx
hf_username = <your-username>
control_json_url = https://<host>/metadata/control.json
wallet_password = <your-wallet-password>
```

## 4. Build OpenPi Runner Docker Image

```bash
cd openpi-runner
docker build -t robot-train-openpi:latest .
cd ..
```

> **One universal image** - no need to rebuild for custom training strategies.
> Mount your strategy script via volume + set `CUSTOM_TRAIN` env var (see below).

## 5. Custom Training Strategy (Optional)

Miners can use their own training logic by configuring `custom_train_script` in `miner.yaml`.

### Default: use the built-in strategy

A minimal training script (`custom_train_script/simple_strategy-*.py`) is included.
Place your strategy file in `custom_train_script/` and configure the path:

```bash
# Use the simple strategy (generates valid LoRA adapter)
python miner.py --config miner.yaml
```

### Custom: configure your own training script

Edit `miner.yaml` and set `custom_train_script` to the absolute path of your strategy:

```yaml
# miner.yaml
custom_train_script: "/path/to/custom_train_script/my_strategy.py"
```

Place your script in `custom_train_script/` and reference its absolute path.
The miner will:
1. Verify the script exists
2. Use it as the training entrypoint
3. Container runs your script instead of the default

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

See `custom_train_script/simple_strategy-*.py` in the repo for complete examples that
generate valid adapter files.

## 6. Run Miner

The workflow is split into two stages:

### Step 1-2: Train (miner.py)

```bash
# Prep + training, then exits
python miner.py --config miner.yaml
```

After training completes, state is saved to `state/round_N.json`.

### Step 3-5: Upload → Burn → Announce (rt.py)

```bash
# Full pipeline: upload → burn → announce
python rt.py submit --config miner.yaml

# Or run individual steps (recovery/debugging only — see warning below):
python rt.py upload --config miner.yaml --round 1
python rt.py burn --config miner.yaml
python rt.py announce --config miner.yaml --round 1 --repo <hf_repo> --url <hf_url>
```

> **⚠️ Do not split burn and announce.** The backend enforces a burn→commitment window of **10 blocks (~2 minutes)**; exceeding it rejects the submission and the burned TAO is not refunded. This prevents burn replay (paying once, submitting later or repeatedly). Use one-shot `rt.py submit` — the individual-step commands are for recovery and debugging only.

### Expected Log Output (miner.py)

```
🦞 π0.5 Miner started | hotkey=<hotkey> | HF=<username>
[main] Starting training Round 1
[round 1] 📦 Step 1/2: Preparation
[round 1] ✅ Preparation complete
[round 1] 🚀 Step 2/2: Model Training
[train_vla] Downloading dataset...
[train_vla] Starting training...
[round 1] 📊 Training complete | final_loss=0.xxx
[round 1] ✅ Training complete — model saved at ./tmp/robot_train_vla_miner/round_1
[round 1] 📝 Run 'python rt.py submit --round 1' for steps 3-5
```

### Expected Log Output (rt.py submit)

```
🦞 rt.py submit | round=1
[rt] Step 3/3: Upload to HuggingFace
✅ Uploaded: https://huggingface.co/<user>/pi05-<hotkey_short>
[rt] Step 4/3: Stake Burn Payment
✅ Burn submitted: tx=0x...
[rt] Step 5/3: Chain Commitment
✅ Commitment submitted | block=7500900 ext=0x...
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
WorkingDirectory=/home/robot-train/robot-train-subnet
ExecStart=/home/robot-train/robot-train-subnet/.venv/bin/python miner.py --config miner.yaml
Environment=PATH=/home/robot-train/robot-train-subnet/.venv/bin:/usr/bin:/bin
Environment=HF_TOKEN=<your-token>

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable robot-train-miner.service
sudo systemctl start robot-train-miner.service

# Monitor
tail -f /home/robot-train/robot-train-subnet/logs/miner.log
```

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
```

### Training container fails
```bash
# Check openpi-runner image
docker images | grep robot-train-openpi

# Run manually for debugging
docker run --rm --gpus all -v /data:/data robot-train-openpi:latest nvidia-smi
```

## Important Notes

- `miner.py` runs **once per round** then exits. No polling loop.
- `rt.py submit` runs the post-training pipeline (upload → burn → announce).
- State is saved to `state/round_N.json` — re-running `rt.py submit` resumes from the last completed step.
- Backend scanner picks up submissions within ~60 seconds.
- Payment config (`burn_rate_tao`, `limit_price_rao`) comes from owner's `control.json`, not miner.yaml.
