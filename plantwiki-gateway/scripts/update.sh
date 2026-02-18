#!/usr/bin/env bash
set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
  echo "This script must be run as root (sudo ./scripts/update.sh)." >&2
  exit 1
fi

APP_NAME="plantwiki-gateway"
INSTALL_DIR="/opt/${APP_NAME}"
SERVICE_FILE="/etc/systemd/system/${APP_NAME}.service"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

mkdir -p "${INSTALL_DIR}"
rm -rf "${INSTALL_DIR}/gateway" "${INSTALL_DIR}/config"
cp -a "${SRC_DIR}/gateway" "${INSTALL_DIR}/gateway"
cp -a "${SRC_DIR}/config" "${INSTALL_DIR}/config"
chown -R root:root "${INSTALL_DIR}"
chmod -R go-w "${INSTALL_DIR}"

install -o root -g root -m 644 "${SRC_DIR}/systemd/${APP_NAME}.service" "${SERVICE_FILE}"
systemctl daemon-reload
systemctl restart "${APP_NAME}"

echo "Update complete."
echo "Check status with: systemctl status ${APP_NAME}"
