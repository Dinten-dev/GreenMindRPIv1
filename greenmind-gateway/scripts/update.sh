#!/bin/bash
set -e

APP_NAME="greenmind-gateway"
INSTALL_DIR="/var/lib/$APP_NAME"
SERVICE_NAME="$APP_NAME.service"

echo "Updating $APP_NAME..."

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
SRC_ROOT="$SCRIPT_DIR/.."

# Stop service
echo "Stopping service..."
systemctl stop "$SERVICE_NAME"

# Update Code
echo "Updating code..."
cp "$SRC_ROOT/src/gateway.py" "$INSTALL_DIR/src/"

# Ensure Venv permissions are correct (sometimes messed up by manual edits)
chown -R greenmind:greenmind "$INSTALL_DIR"

# Start service
echo "Starting service..."
systemctl start "$SERVICE_NAME"

echo "Update complete."
