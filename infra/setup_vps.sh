#!/bin/bash
# ============================================================
# Epic Fury IBKR Mirror — VPS Setup Script
# Runs on a fresh Ubuntu 22.04 EC2 instance.
# Installs: Java, Xvfb, IB Gateway, IBC, Python deps, systemd services
# Usage:
#   ssh ubuntu@<EC2-IP>
#   git clone https://github.com/theleesbuildwithai/epic-fury-stock-analyzer.git
#   cd epic-fury-stock-analyzer/infra
#   bash setup_vps.sh
# ============================================================
set -euo pipefail

log() { echo -e "\e[1;36m[$(date +%H:%M:%S)]\e[0m $*"; }
err() { echo -e "\e[1;31m[ERROR]\e[0m $*" >&2; exit 1; }

[[ $EUID -eq 0 ]] && err "Run as ubuntu user, not root. Use: bash setup_vps.sh"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IBC_DIR="/opt/ibc"
GATEWAY_DIR="/opt/Jts/ibgateway"
USER_HOME="/home/ubuntu"
BACKEND_DIR="$REPO_ROOT/backend"

log "Repo root: $REPO_ROOT"
log "User: $(whoami), home: $USER_HOME"

# ─── 1. System packages ────────────────────────────────────────
log "Installing system packages..."
sudo apt-get update -y
sudo apt-get install -y \
    openjdk-11-jre-headless \
    xvfb x11-utils \
    wget unzip curl \
    python3 python3-pip python3-venv \
    git build-essential \
    tmux htop jq

# ─── 2. Download and install IB Gateway (latest stable) ────────
log "Installing IB Gateway..."
if [[ ! -d "$GATEWAY_DIR" ]]; then
    cd /tmp
    # Latest stable Linux installer (offline)
    wget -q "https://download2.interactivebrokers.com/installers/ibgateway/stable-standalone/ibgateway-stable-standalone-linux-x64.sh" \
        -O ibgateway-installer.sh
    chmod +x ibgateway-installer.sh
    # Silent install to /opt/Jts (default)
    sudo bash ibgateway-installer.sh -q -dir /opt/Jts/ibgateway
    log "IB Gateway installed to $GATEWAY_DIR"
else
    log "IB Gateway already installed — skipping"
fi

# ─── 3. Install IBC (Interactive Brokers Controller) ───────────
log "Installing IBC..."
if [[ ! -d "$IBC_DIR" ]]; then
    IBC_VERSION="3.21.2"  # Pin to known-good version
    cd /tmp
    wget -q "https://github.com/IbcAlpha/IBC/releases/download/${IBC_VERSION}/IBCLinux-${IBC_VERSION}.zip" \
        -O ibc.zip
    sudo mkdir -p "$IBC_DIR"
    sudo unzip -q ibc.zip -d "$IBC_DIR"
    sudo chmod +x "$IBC_DIR"/*.sh
    sudo chown -R ubuntu:ubuntu "$IBC_DIR"
    log "IBC installed to $IBC_DIR"
else
    log "IBC already installed — skipping"
fi

# ─── 4. Copy IBC config template (user will fill in creds) ─────
log "Setting up IBC config..."
CONFIG_TEMPLATE="$REPO_ROOT/infra/templates/ibc_config.ini"
CONFIG_TARGET="$IBC_DIR/config.ini"
if [[ ! -f "$CONFIG_TARGET" ]]; then
    cp "$CONFIG_TEMPLATE" "$CONFIG_TARGET"
    chmod 600 "$CONFIG_TARGET"
    log "IBC config copied to $CONFIG_TARGET (chmod 600)"
    log "!! YOU MUST EDIT THIS FILE WITH YOUR IBKR USERNAME AND PASSWORD !!"
else
    log "IBC config already exists — not overwriting"
fi

# ─── 5. Python venv + backend deps ─────────────────────────────
log "Setting up Python venv for backend..."
cd "$BACKEND_DIR"
if [[ ! -d "venv" ]]; then
    python3 -m venv venv
fi
# shellcheck disable=SC1091
source venv/bin/activate
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt || {
    log "Some pinned versions failed — installing latest compatible"
    pip install --quiet fastapi uvicorn yfinance pandas numpy scipy scikit-learn \
        apscheduler pytz ib_insync nest_asyncio feedparser requests beautifulsoup4 \
        boto3 statsmodels
}
deactivate
log "Python deps installed"

# ─── 6. Install systemd services ───────────────────────────────
log "Installing systemd services..."
for svc in ibkr-gateway mirror-backend; do
    src="$REPO_ROOT/infra/systemd/${svc}.service"
    dst="/etc/systemd/system/${svc}.service"
    sudo cp "$src" "$dst"
    log "Installed $dst"
done
sudo systemctl daemon-reload
sudo systemctl enable ibkr-gateway.service
sudo systemctl enable mirror-backend.service
log "Services enabled (will start on boot)"

# ─── 7. Final instructions ─────────────────────────────────────
cat <<EOF

╔════════════════════════════════════════════════════════════════╗
║                    SETUP COMPLETE                              ║
╠════════════════════════════════════════════════════════════════╣
║                                                                ║
║  NEXT STEPS:                                                   ║
║                                                                ║
║  1. Edit IBC config with your IBKR credentials:                ║
║     nano $IBC_DIR/config.ini                                   ║
║                                                                ║
║     Set these three lines:                                     ║
║       IbLoginId=YOUR_USERNAME                                  ║
║       IbPassword=YOUR_PASSWORD                                 ║
║       TradingMode=live       (or 'paper')                      ║
║                                                                ║
║  2. Start services:                                            ║
║       sudo systemctl start ibkr-gateway                        ║
║       sudo systemctl start mirror-backend                      ║
║                                                                ║
║  3. Complete 2FA on your phone when IBKR Mobile prompts.       ║
║     The request appears within 60 seconds of starting.         ║
║                                                                ║
║  4. Verify connection:                                         ║
║       curl http://localhost:8000/api/ibkr/status | jq          ║
║                                                                ║
║  5. Watch logs:                                                ║
║       sudo journalctl -u mirror-backend -f                     ║
║       sudo journalctl -u ibkr-gateway -f                       ║
║                                                                ║
║  DAILY CYCLE:                                                  ║
║   IBKR requires daily 2FA reconfirmation. Approve the push     ║
║   notification that arrives once per day (~10 sec).            ║
║                                                                ║
║  SAFETY:                                                       ║
║    KILL SWITCH:                                                ║
║      curl -X POST http://localhost:8000/api/ibkr/kill-switch   ║
║    PAUSE MIRROR:                                               ║
║      sudo systemctl stop mirror-backend                        ║
║                                                                ║
╚════════════════════════════════════════════════════════════════╝

EOF
