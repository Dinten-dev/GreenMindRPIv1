#!/usr/bin/env bash
# GreenMind Gateway – Raspberry Pi Installation Script
# Usage: sudo bash scripts/install.sh
set -euo pipefail

INSTALL_DIR="/opt/greenmind/gateway"
DATA_DIR="/opt/greenmind/data"
LOG_DIR="${DATA_DIR}/logs"
SERVICE_NAME="greenmind-gateway"

echo "🌿 GreenMind Gateway Installer"
echo "================================"

# 1. System dependencies
echo "📦 Installing system dependencies..."
apt-get update -qq
apt-get install -y -qq network-manager python3-venv sqlite3

# 2. Ensure NetworkManager is the active network backend
echo "🔧 Enabling NetworkManager..."
systemctl enable --now NetworkManager || true

# 3. Create directories
echo "📁 Creating directories..."
mkdir -p "${INSTALL_DIR}"
mkdir -p "${DATA_DIR}"
mkdir -p "${LOG_DIR}"

# 4. Copy source (if running from the repo)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [ -d "${SCRIPT_DIR}/src" ]; then
    echo "📋 Copying source files to ${INSTALL_DIR}..."
    cp -r "${SCRIPT_DIR}/src" "${INSTALL_DIR}/"
    cp -r "${SCRIPT_DIR}/requirements.txt" "${INSTALL_DIR}/"
    [ -f "${SCRIPT_DIR}/.env" ] && cp "${SCRIPT_DIR}/.env" "${INSTALL_DIR}/.env"
fi

# 5. Python virtual environment
echo "🐍 Setting up Python virtual environment..."
python3 -m venv "${INSTALL_DIR}/venv"
"${INSTALL_DIR}/venv/bin/pip" install --upgrade pip -q
"${INSTALL_DIR}/venv/bin/pip" install -r "${INSTALL_DIR}/requirements.txt" -q

# 6. Set permissions on data directory
chmod 700 "${DATA_DIR}"

# 7. Install systemd service
echo "⚙️  Installing systemd service..."
cp "${SCRIPT_DIR}/systemd/${SERVICE_NAME}.service" "/etc/systemd/system/${SERVICE_NAME}.service"
systemctl daemon-reload
systemctl enable "${SERVICE_NAME}"

echo ""
echo "✅ Installation complete!"
echo ""
echo "   Start the service:   sudo systemctl start ${SERVICE_NAME}"
echo "   View logs:           sudo journalctl -u ${SERVICE_NAME} -f"
echo "   Service status:      sudo systemctl status ${SERVICE_NAME}"
echo ""
echo "   Data directory:      ${DATA_DIR}"
echo "   Log directory:       ${LOG_DIR}"
echo "   Secrets file:        ${DATA_DIR}/secrets.json"
echo ""
echo "   To factory-reset:    sudo touch /boot/reset_greenmind.txt && sudo reboot"
echo ""
