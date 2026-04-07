#!/bin/bash
set -e

APP_NAME="greenmind-gateway"
INSTALL_DIR="/opt/greenmind/gateway"
SERVICE_NAME="$APP_NAME.service"

echo "Updating $APP_NAME..."

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
SRC_ROOT="$SCRIPT_DIR/.."

# Stop service
echo "Stopping service..."
systemctl stop "$SERVICE_NAME"

# Update code
echo "Updating source code..."
cp -r "$SRC_ROOT/src/" "$INSTALL_DIR/src/"
cp "$SRC_ROOT/requirements.txt" "$INSTALL_DIR/requirements.txt"

# Reinstall dependencies (in case requirements changed)
echo "Updating dependencies..."
"$INSTALL_DIR/venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt" -q

# Start service
echo "Starting service..."
systemctl start "$SERVICE_NAME"

echo "Update complete."
