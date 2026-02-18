#!/usr/bin/env bash
set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
  echo "This script must be run as root (sudo ./scripts/uninstall.sh [--purge])." >&2
  exit 1
fi

APP_NAME="plantwiki-gateway"
APP_USER="plantwiki"
INSTALL_DIR="/opt/${APP_NAME}"
CONFIG_DIR="/etc/${APP_NAME}"
DATA_DIR="/var/lib/${APP_NAME}"
SERVICE_FILE="/etc/systemd/system/${APP_NAME}.service"

PURGE="false"
if [[ "${1:-}" == "--purge" ]]; then
  PURGE="true"
fi

if systemctl list-unit-files | grep -q "^${APP_NAME}.service"; then
  systemctl stop "${APP_NAME}" || true
  systemctl disable "${APP_NAME}" || true
fi

rm -f "${SERVICE_FILE}"
systemctl daemon-reload
rm -rf "${INSTALL_DIR}"

if [[ "${PURGE}" == "true" ]]; then
  rm -rf "${CONFIG_DIR}" "${DATA_DIR}"
  if id -u "${APP_USER}" >/dev/null 2>&1; then
    userdel "${APP_USER}" || true
  fi
  echo "Uninstalled and purged ${APP_NAME}."
else
  echo "Uninstalled ${APP_NAME}."
  echo "Configuration and queue data kept in ${CONFIG_DIR} and ${DATA_DIR}."
  echo "Run with --purge to remove them as well."
fi
