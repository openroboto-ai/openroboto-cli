#!/bin/bash
# deploy_miner.sh — Miner auto-deploy script
# For Ubuntu 22.04 / 24.04
# Run as root
#
# Usage: sudo bash scripts/deploy_miner.sh

set -e

DEPLOY_DIR="/data/robot-train"
# The default used to be https://github.com/your-org/robot-train-subnet.git — a
# placeholder for a repository that does not exist, so anyone following the docs
# failed at the clone step. This is the real public repository.
REPO_URL="${REPO_URL:-https://github.com/openroboto-ai/openroboto-cli.git}"
REPO_BRANCH="${REPO_BRANCH:-main}"
CLONE_DIR="$DEPLOY_DIR/openroboto-cli"

echo "============================================"
echo "  RobotTrain π₀.₅ Miner — Auto Deploy"
echo "============================================"

# ─── Prerequisites ────────────────────────────────────
if [ "$(id -u)" -ne 0 ]; then
    echo "❌ Please run as root: sudo bash $0"
    exit 1
fi

if [ ! -f /etc/os-release ]; then
    echo "❌ Cannot detect operating system"
    exit 1
fi

source /etc/os-release
if [[ "$VERSION_ID" != "22.04" && "$VERSION_ID" != "24.04" ]]; then
    echo "⚠️  Not tested on Ubuntu 22.04 or 24.04 (current: $VERSION_ID)"
    read -p "Continue? (y/N) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        exit 1
    fi
fi

# ─── 1. System Dependencies ──────────────────────────────────
echo ""
echo "📦 Installing system dependencies..."
apt update
apt upgrade -y

apt install -y git curl wget build-essential software-properties-common \
    libgl1-mesa-glx libglib2.0-0 libsm6 libxrender1 libxext6 \
    ffmpeg libavcodec-dev libavformat-dev libavutil-dev \
    libgmp-dev jq sqlite3

# Python 3.11
if ! command -v python3.11 &>/dev/null; then
    echo "🐍 Installing Python 3.11..."
    add-apt-repository -y ppa:deadsnakes/ppa
    apt install -y python3.11 python3.11-venv python3.11-dev
fi

# ─── 2. NVIDIA Driver Check ─────────────────────────
echo ""
echo "🔍 Checking NVIDIA driver..."
if command -v nvidia-smi &>/dev/null; then
    nvidia-smi --query-gpu=name,memory.total --format=csv,noheader
else
    echo "⚠️  nvidia-smi not detected. Install driver?"
    read -p "Install nvidia-driver-535? (y/N) " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        apt install -y nvidia-driver-535
        echo "✅ Driver installed, please reboot and continue"
        exit 0
    else
        echo "⚠️  Skipping driver installation. GPU features will not be available."
    fi
fi

# ─── 3. Docker Installation ───────────────────────────────
echo ""
echo "🐳 Installing Docker..."
if command -v docker &>/dev/null; then
    echo "✅ Docker already installed: $(docker --version)"
else
    curl -fsSL https://get.docker.com | sh
fi

# NVIDIA Container Toolkit
if ! command -v nvidia-ctk &>/dev/null; then
    echo "🔧 Installing NVIDIA Container Toolkit..."
    curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | \
        gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
    curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | \
        sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \
        tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
    apt update
    apt install -y nvidia-container-toolkit
    nvidia-ctk runtime configure --runtime=docker
    systemctl restart docker
fi

# Verify GPU Docker
echo "🧪 Verifying GPU Docker..."
if docker run --rm --gpus all nvidia/cuda:12.1.0-runtime-ubuntu22.04 nvidia-smi &>/dev/null; then
    echo "✅ GPU Docker working"
else
    echo "⚠️  GPU Docker verification failed, please check nvidia-container-toolkit"
fi

# ─── 4. Create Deploy User and Directories ─────────────────────────
echo ""
echo "👤 Creating deploy user..."
if ! id robot-train &>/dev/null; then
    useradd -m -s /bin/bash robot-train
fi

mkdir -p "$DEPLOY_DIR"
mkdir -p "$DEPLOY_DIR/logs"
mkdir -p "$DEPLOY_DIR/run"
mkdir -p "$DEPLOY_DIR/data/models"
mkdir -p "$DEPLOY_DIR/data/episodes"
mkdir -p "$DEPLOY_DIR/data/output"
mkdir -p "$DEPLOY_DIR/scripts"
chown -R robot-train:robot-train "$DEPLOY_DIR"

# ─── 5. Clone Repository ──────────────────────────────────
echo ""
echo "📂 Cloning repository..."
if [ ! -d "$CLONE_DIR/.git" ]; then
    sudo -u robot-train git clone --branch "$REPO_BRANCH" "$REPO_URL" "$CLONE_DIR"
else
    echo "✅ Repository already exists, skipping clone"
    sudo -u robot-train git -C "$CLONE_DIR" pull
fi

# ─── 6. Python Virtual Environment ───────────────────────────
echo ""
echo "🐍 Creating Python virtual environment..."
if [ ! -d "$DEPLOY_DIR/venv" ]; then
    sudo -u robot-train python3.11 -m venv "$DEPLOY_DIR/venv"
fi

# The CLI itself is a pip package (`openroboto`); openpi lives only inside the
# training container, never in this interpreter (numpy<2.0 vs numpy>=2.0).
sudo -u robot-train bash -c "
source $DEPLOY_DIR/venv/bin/activate
pip install --upgrade pip
pip install $CLONE_DIR
"

# ─── 7. Configuration File ──────────────────────────────────
echo ""
echo "⚙️  Configuration file..."
# `openroboto init` writes miner.yaml plus a training strategy script, so the
# miner does not have to copy example files by hand.
if [ ! -f "$DEPLOY_DIR/miner.yaml" ]; then
    sudo -u robot-train "$DEPLOY_DIR/venv/bin/openroboto" init "$DEPLOY_DIR"
    echo "📝 Please edit the config: sudo -u robot-train nano $DEPLOY_DIR/miner.yaml"
else
    echo "✅ miner.yaml already exists"
fi

# ─── 8. systemd Service ──────────────────────────────
echo ""
echo "🔧 Creating systemd service..."
cat > /etc/systemd/system/robot-train-miner.service << SERVICEEOF
[Unit]
Description=RobotTrain π₀.₅ Miner
After=network-online.target docker.service
Wants=docker.service

[Service]
Type=simple
User=robot-train
Group=robot-train
WorkingDirectory=$DEPLOY_DIR
Environment="PATH=$DEPLOY_DIR/venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin"
ExecStart=$DEPLOY_DIR/venv/bin/openroboto train --config miner.yaml
Restart=always
RestartSec=30
StandardOutput=append:$DEPLOY_DIR/logs/miner.log
StandardError=append:$DEPLOY_DIR/logs/miner_error.log
LimitNOFILE=65536
LimitMEMLOCK=infinity

[Install]
WantedBy=multi-user.target
SERVICEEOF

systemctl daemon-reload

# ─── Done ─────────────────────────────────────────
echo ""
echo "============================================"
echo "  ✅ Deploy Complete!"
echo "============================================"
echo ""
echo "Next steps:"
echo "  1. Edit config: sudo -u robot-train nano $DEPLOY_DIR/miner.yaml"
echo "  2. Check the environment: sudo -u robot-train $DEPLOY_DIR/venv/bin/openroboto doctor"
echo "  3. Build the training image: sudo -u robot-train $DEPLOY_DIR/venv/bin/openroboto build"
echo "  4. Start service: sudo systemctl start robot-train-miner"
echo "  5. View logs: tail -f $DEPLOY_DIR/logs/miner.log"
echo ""
