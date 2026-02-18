#!/usr/bin/env bash
set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
  echo "This script must be run as root (sudo ./scripts/install.sh)." >&2
  exit 1
fi

APP_NAME="plantwiki-gateway"
APP_USER="plantwiki"
APP_GROUP="plantwiki"
INSTALL_DIR="/opt/${APP_NAME}"
CONFIG_DIR="/etc/${APP_NAME}"
DATA_DIR="/var/lib/${APP_NAME}"
SERVICE_FILE="/etc/systemd/system/${APP_NAME}.service"

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

create_user() {
  if ! id -u "${APP_USER}" >/dev/null 2>&1; then
    useradd --system --home /nonexistent --shell /usr/sbin/nologin "${APP_USER}"
  fi
}

install_app_files() {
  mkdir -p "${INSTALL_DIR}" "${CONFIG_DIR}" "${DATA_DIR}"

  rm -rf "${INSTALL_DIR}/gateway" "${INSTALL_DIR}/config"
  cp -a "${SRC_DIR}/gateway" "${INSTALL_DIR}/gateway"
  cp -a "${SRC_DIR}/config" "${INSTALL_DIR}/config"

  chown -R root:root "${INSTALL_DIR}"
  chmod -R go-w "${INSTALL_DIR}"

  chown -R "${APP_USER}:${APP_GROUP}" "${DATA_DIR}"
  chmod 750 "${DATA_DIR}"

  if [[ ! -f "${CONFIG_DIR}/config.env" ]]; then
    install -o root -g root -m 600 "${SRC_DIR}/config/config.env.example" "${CONFIG_DIR}/config.env"
    echo "Created ${CONFIG_DIR}/config.env from template. Please edit it before starting the service."
  else
    chown root:root "${CONFIG_DIR}/config.env"
    chmod 600 "${CONFIG_DIR}/config.env"
  fi
}

install_service() {
  install -o root -g root -m 644 "${SRC_DIR}/systemd/${APP_NAME}.service" "${SERVICE_FILE}"
  systemctl daemon-reload
  systemctl enable "${APP_NAME}"
}

configure_firewall_if_available() {
  if command -v ufw >/dev/null 2>&1; then
    ufw default deny incoming || true
    ufw default allow outgoing || true
    ufw allow 22/tcp || true
    ufw allow 8081/tcp || true
    ufw --force enable || true
  fi
}

print_hardening_hints() {
  cat <<MSG
Installation complete.

Next steps:
1) Edit ${CONFIG_DIR}/config.env and set BACKEND_URL + DEVICE_API_KEY.
2) Start service: systemctl start ${APP_NAME}
3) Check status: systemctl status ${APP_NAME}
4) Follow logs: journalctl -u ${APP_NAME} -f

Recommended SSH hardening (manual):
- Set PasswordAuthentication no in /etc/ssh/sshd_config
- Ensure PubkeyAuthentication yes
- Restart SSH: systemctl restart ssh
MSG
}

create_user
install_app_files
install_service
configure_firewall_if_available
print_hardening_hints
