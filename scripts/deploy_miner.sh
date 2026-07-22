#!/bin/bash
# deploy_miner.sh — Miner auto-deploy script
# For Ubuntu 22.04 / 24.04
# Run as root
#
# Usage: sudo bash scripts/deploy_miner.sh

set -e

DEPLOY_DIR="/data/robot-train"
REPO_URL="${REPO_URL:-https://github.com/your-org/robot-train-subnet.git}"
REPO_BRANCH="${REPO_BRANCH:-main}"

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
if [ ! -d "$DEPLOY_DIR/robot-train-subnet/.git" ]; then
    sudo -u robot-train git clone --branch "$REPO_BRANCH" "$REPO_URL" "$DEPLOY_DIR/robot-train-subnet"
else
    echo "✅ Repository already exists, skipping clone"
    sudo -u robot-train git -C "$DEPLOY_DIR/robot-train-subnet" pull
fi

# ─── 6. Python Virtual Environment ───────────────────────────
echo ""
echo "🐍 Creating Python virtual environment..."
if [ ! -d "$DEPLOY_DIR/venv" ]; then
    sudo -u robot-train python3.11 -m venv "$DEPLOY_DIR/venv"
fi

sudo -u robot-train bash -c "
source $DEPLOY_DIR/venv/bin/activate
pip install --upgrade pip
cd $DEPLOY_DIR/robot-train-subnet
pip install -r requirements.txt
pip install git+https://github.com/Physical-Intelligence/openpi.git@main
"

# ─── 7. Configuration File ──────────────────────────────────
echo ""
echo "⚙️  Configuration file..."
if [ ! -f "$DEPLOY_DIR/robot-train-subnet/config.yaml" ]; then
    sudo -u robot-train cp "$DEPLOY_DIR/robot-train-subnet/config.example.yaml" "$DEPLOY_DIR/robot-train-subnet/config.yaml"
    echo "📝 Please edit the config: sudo -u robot-train nano $DEPLOY_DIR/robot-train-subnet/config.yaml"
else
    echo "✅ config.yaml already exists"
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
WorkingDirectory=$DEPLOY_DIR/robot-train-subnet
Environment="PATH=$DEPLOY_DIR/venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin"
Environment="REGISTRY_PASSWORD="
ExecStart=$DEPLOY_DIR/venv/bin/python miner_vla.py --config config.yaml --registry docker.io --registry-username "" --registry-password \$REGISTRY_PASSWORD
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
echo "  1. Edit config: sudo -u robot-train nano $DEPLOY_DIR/robot-train-subnet/config.yaml"
echo "  2. Set Docker password: sudo systemctl edit robot-train-miner"
echo "  3. Start service: sudo systemctl start robot-train-miner"
echo "  4. Check status: sudo systemctl status robot-train-miner"
echo "  5. View logs: tail -f $DEPLOY_DIR/logs/miner.log"
echo ""
