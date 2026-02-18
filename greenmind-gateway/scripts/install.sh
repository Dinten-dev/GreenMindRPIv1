#!/bin/bash
set -e

# Configuration
APP_NAME="greenmind-gateway"
INSTALL_DIR="/var/lib/$APP_NAME"
CONFIG_DIR="/etc/$APP_NAME"
SERVICE_NAME="$APP_NAME.service"

echo "Installing $APP_NAME..."

# 1. Create User
if ! id -u greenmind > /dev/null 2>&1; then
    echo "Creating user greenmind..."
    useradd -r -s /bin/false greenmind
fi

# 2. Create Directories
echo "Creating directories..."
mkdir -p "$INSTALL_DIR/src"
mkdir -p "$CONFIG_DIR"
chown -R greenmind:greenmind "$INSTALL_DIR"
chown -R greenmind:greenmind "$CONFIG_DIR"

# 3. Copy Application Files
echo "Copying application files..."
# Assuming script is run from project root or checks relative path
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
SRC_ROOT="$SCRIPT_DIR/.."

cp "$SRC_ROOT/src/gateway.py" "$INSTALL_DIR/src/"

# 4. Setup Python Environment
if [ ! -d "$INSTALL_DIR/venv" ]; then
    echo "Setting up Python venv..."
    python3 -m venv "$INSTALL_DIR/venv"
    "$INSTALL_DIR/venv/bin/pip" install --upgrade pip
    "$INSTALL_DIR/venv/bin/pip" install fastapi uvicorn requests python-dotenv
fi

# 5. Install Config
if [ ! -f "$CONFIG_DIR/config.env" ]; then
    echo "Installing default config..."
    cp "$SRC_ROOT/config/config.env.example" "$CONFIG_DIR/config.env"
    echo "PLEASE EDIT $CONFIG_DIR/config.env WITH CORRECT VALUES"
fi

# 6. Install Systemd Service
echo "Installing systemd service..."
cp "$SRC_ROOT/systemd/$SERVICE_NAME" "/etc/systemd/system/"
systemctl daemon-reload
systemctl enable "$SERVICE_NAME"

echo "Fixing permissions..."
chown -R greenmind:greenmind "$INSTALL_DIR"
chown -R greenmind:greenmind "$CONFIG_DIR"
chmod 700 "$INSTALL_DIR"
chmod 600 "$CONFIG_DIR/config.env"

echo "Installation complete."
echo "To start service: systemctl start $SERVICE_NAME"
echo "Check status: systemctl status $SERVICE_NAME"
echo "Check logs: journalctl -u $SERVICE_NAME -f"
