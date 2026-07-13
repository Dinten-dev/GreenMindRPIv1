#!/usr/bin/env bash
# ============================================================================
# GreenMind Raspberry Pi Gateway — Production Installer
# ============================================================================
#
# One-liner install:
#   curl -fsSL https://raw.githubusercontent.com/Dinten-dev/GreenMindRPIv1/master/greenmind-gateway/install-gateway.sh | sudo bash
#
# Features:
#   - Idempotent: safe to run multiple times
#   - Creates dedicated 'greenmind' system user (non-root)
#   - Clones/updates repo, sets up Python venv, systemd services
#   - Interactive credential prompting (CLOUD_API_URL + pairing)
#   - OTA update agent with restricted sudo
#   - Log rotation via logrotate
#   - Daily OTA check cron job
#   - Architecture detection (warns on non-ARM)
#
# Requirements:
#   - Raspberry Pi OS (Debian Bookworm, 64-bit)
#   - Root privileges (sudo)
#   - Internet connection
# ============================================================================

set -euo pipefail

# ── Constants ────────────────────────────────────────────────────────────────

readonly SCRIPT_VERSION="1.0.0"
readonly REPO_URL="https://github.com/Dinten-dev/GreenMindRPIv1.git"
readonly REPO_BRANCH="master"
readonly INSTALL_BASE="/opt/greenmind"
readonly REPO_DIR="${INSTALL_BASE}/repo"
readonly CURRENT_LINK="${INSTALL_BASE}/current"
readonly DATA_DIR="${INSTALL_BASE}/data"
readonly LOG_DIR="${DATA_DIR}/logs"
readonly WAV_DIR="${DATA_DIR}/wav"
readonly CONFIG_DIR="${INSTALL_BASE}/config"
readonly AGENT_DIR="${INSTALL_BASE}/agent"
readonly RELEASES_DIR="${INSTALL_BASE}/releases"
readonly BACKUPS_DIR="${INSTALL_BASE}/backups"
readonly ENV_FILE="${INSTALL_BASE}/current/.env"
readonly SERVICE_USER="greenmind"
readonly AGENT_USER="greenmind-agent"
readonly GATEWAY_SERVICE="greenmind-gateway"
readonly AGENT_SERVICE="greenmind-agent"
readonly LOGROTATE_CONF="/etc/logrotate.d/greenmind-gateway"
readonly CRON_FILE="/etc/cron.d/greenmind-ota"
readonly SUDOERS_FILE="/etc/sudoers.d/greenmind-agent"

# ── Colors ───────────────────────────────────────────────────────────────────

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m' # No Color

# ── Helper Functions ─────────────────────────────────────────────────────────

info()    { echo -e "${BLUE}ℹ ${NC} $*"; }
success() { echo -e "${GREEN}✅${NC} $*"; }
warn()    { echo -e "${YELLOW}⚠️ ${NC} $*"; }
error()   { echo -e "${RED}❌${NC} $*" >&2; }
step()    { echo -e "\n${CYAN}${BOLD}── $* ──${NC}"; }

die() {
    error "$*"
    echo -e "${RED}Installation aborted.${NC}" >&2
    exit 1
}

# Cleanup on error
cleanup() {
    local exit_code=$?
    if [ $exit_code -ne 0 ]; then
        echo ""
        error "Installation failed (exit code: ${exit_code})."
        error "Check the output above for details."
        echo -e "${YELLOW}Partial installation may remain at ${INSTALL_BASE}${NC}"
        echo -e "${YELLOW}To retry: re-run this script.${NC}"
    fi
}
trap cleanup EXIT

# ── Pre-flight Checks ───────────────────────────────────────────────────────

banner() {
    echo ""
    echo -e "${GREEN}${BOLD}"
    echo "  ╔══════════════════════════════════════════════════════╗"
    echo "  ║                                                      ║"
    echo "  ║   🌿 GreenMind Gateway Installer v${SCRIPT_VERSION}            ║"
    echo "  ║                                                      ║"
    echo "  ║   Self-thinking greenhouse edge gateway               ║"
    echo "  ║   https://green-mind.ch                               ║"
    echo "  ║                                                      ║"
    echo "  ╚══════════════════════════════════════════════════════╝"
    echo -e "${NC}"
}

check_root() {
    if [ "$(id -u)" -ne 0 ]; then
        die "This script must be run as root. Use: sudo bash install-gateway.sh"
    fi
}

check_architecture() {
    local arch
    arch="$(uname -m)"
    case "${arch}" in
        aarch64|arm64|armv7l|armv6l)
            success "Architecture: ${arch} (ARM — Raspberry Pi detected)"
            ;;
        *)
            warn "Architecture: ${arch} — this does not appear to be a Raspberry Pi."
            warn "This installer is designed for Raspberry Pi OS (Debian Bookworm, ARM)."
            echo ""
            read -r -p "Continue anyway? [y/N] " confirm
            if [[ ! "${confirm}" =~ ^[Yy]$ ]]; then
                die "Installation cancelled by user."
            fi
            ;;
    esac
}

check_os() {
    if [ -f /etc/os-release ]; then
        # shellcheck disable=SC1091
        . /etc/os-release
        info "OS: ${PRETTY_NAME:-unknown}"
        if [[ "${ID:-}" != "debian" && "${ID:-}" != "raspbian" ]]; then
            warn "Expected Debian/Raspbian, found: ${ID:-unknown}. Continuing anyway."
        fi
    else
        warn "Could not detect OS. Continuing anyway."
    fi
}

check_internet() {
    if ! curl -fsSL --max-time 10 https://github.com > /dev/null 2>&1; then
        die "No internet connection. Cannot reach github.com."
    fi
    success "Internet connectivity confirmed"
}

check_disk_space() {
    local available_mb
    available_mb=$(df -m /opt 2>/dev/null | awk 'NR==2 {print $4}')
    if [ -n "${available_mb}" ] && [ "${available_mb}" -lt 500 ]; then
        warn "Low disk space: ${available_mb} MB available on /opt (recommended: 500+ MB)"
    else
        success "Disk space: ${available_mb:-unknown} MB available on /opt"
    fi
}

# ── Step 1: System Update ────────────────────────────────────────────────────

system_update() {
    step "1/11 — System Update"

    info "Updating package lists..."
    apt-get update -qq

    info "Upgrading installed packages..."
    DEBIAN_FRONTEND=noninteractive apt-get upgrade -y -qq \
        -o Dpkg::Options::="--force-confdef" \
        -o Dpkg::Options::="--force-confold"

    success "System updated"
}

# ── Step 2: Install Dependencies ─────────────────────────────────────────────

install_dependencies() {
    step "2/11 — Installing Dependencies"

    local packages=(
        python3
        python3-pip
        python3-venv
        git
        curl
        jq
        sqlite3
        network-manager
        logrotate
    )

    info "Installing: ${packages[*]}"
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${packages[@]}"

    # Verify Python version
    local python_version
    python_version="$(python3 --version 2>&1)"
    local python_minor
    python_minor="$(python3 -c 'import sys; print(sys.version_info.minor)')"
    if [ "${python_minor}" -lt 11 ]; then
        die "Python 3.11+ required, found: ${python_version}"
    fi
    success "Python: ${python_version}"

    # Ensure NetworkManager is active
    if systemctl is-enabled NetworkManager &>/dev/null; then
        info "NetworkManager already enabled"
    else
        info "Enabling NetworkManager..."
        systemctl enable --now NetworkManager || true
    fi

    success "All dependencies installed"
}

# ── Step 3: Create System Users ──────────────────────────────────────────────

create_users() {
    step "3/11 — Creating System Users"

    # Gateway user (runs the main gateway service)
    if id "${SERVICE_USER}" &>/dev/null; then
        info "User '${SERVICE_USER}' already exists"
    else
        useradd --system --shell /usr/sbin/nologin \
            --home-dir "${INSTALL_BASE}" \
            --comment "GreenMind Gateway Service" \
            "${SERVICE_USER}"
        success "Created system user: ${SERVICE_USER}"
    fi

    # Agent user (runs the OTA update agent)
    if id "${AGENT_USER}" &>/dev/null; then
        info "User '${AGENT_USER}' already exists"
    else
        useradd --system --shell /usr/sbin/nologin \
            --home-dir "${AGENT_DIR}" \
            --comment "GreenMind Update Agent" \
            "${AGENT_USER}"
        success "Created system user: ${AGENT_USER}"
    fi

    # Add agent user to greenmind group for shared secrets access
    usermod -aG "${SERVICE_USER}" "${AGENT_USER}" 2>/dev/null || true

    success "System users configured"
}

# ── Step 4: Clone/Update Repository ─────────────────────────────────────────

clone_repository() {
    step "4/11 — Cloning Repository"

    mkdir -p "${INSTALL_BASE}"

    if [ -d "${REPO_DIR}/.git" ]; then
        info "Repository exists, pulling latest changes..."
        git -C "${REPO_DIR}" fetch --quiet origin
        git -C "${REPO_DIR}" reset --hard "origin/${REPO_BRANCH}" --quiet
        git -C "${REPO_DIR}" clean -fd --quiet
        success "Repository updated to latest ${REPO_BRANCH}"
    else
        info "Cloning ${REPO_URL}..."
        rm -rf "${REPO_DIR}"
        git clone --branch "${REPO_BRANCH}" --depth 1 --quiet \
            "${REPO_URL}" "${REPO_DIR}"
        success "Repository cloned to ${REPO_DIR}"
    fi

    # Create the release directory from repo
    local gateway_src="${REPO_DIR}/greenmind-gateway"
    if [ ! -d "${gateway_src}/src" ]; then
        die "Source directory not found: ${gateway_src}/src"
    fi

    # Set up the current symlink to the gateway source
    # (In OTA mode this points to /opt/greenmind/releases/<version>,
    #  but for initial install we point to the repo clone)
    local initial_release="${RELEASES_DIR}/initial"
    mkdir -p "${initial_release}"
    cp -r "${gateway_src}/src" "${initial_release}/"
    cp "${gateway_src}/requirements.txt" "${initial_release}/"
    [ -f "${gateway_src}/.env.example" ] && cp "${gateway_src}/.env.example" "${initial_release}/.env.example"

    # Atomic symlink switch
    ln -sfn "${initial_release}" "${CURRENT_LINK}"
    success "Active release: ${CURRENT_LINK} → ${initial_release}"
}

# ── Step 5: Python Virtual Environment ───────────────────────────────────────

setup_venv() {
    step "5/11 — Python Virtual Environment"

    local venv_dir="${CURRENT_LINK}/venv"

    if [ -d "${venv_dir}" ] && [ -x "${venv_dir}/bin/python" ]; then
        info "Virtual environment exists, upgrading pip..."
        "${venv_dir}/bin/pip" install --upgrade pip --quiet
    else
        info "Creating virtual environment..."
        python3 -m venv "${venv_dir}"
        "${venv_dir}/bin/pip" install --upgrade pip --quiet
    fi

    info "Installing Python dependencies..."
    "${venv_dir}/bin/pip" install -r "${CURRENT_LINK}/requirements.txt" --quiet

    success "Python dependencies installed"
    info "Packages: $(${venv_dir}/bin/pip list --format=columns 2>/dev/null | wc -l) installed"
}

# ── Step 6: Setup Agent ──────────────────────────────────────────────────────

setup_agent() {
    step "6/11 — OTA Update Agent"

    local gateway_src="${REPO_DIR}/greenmind-gateway"
    mkdir -p "${AGENT_DIR}"

    if [ -f "${gateway_src}/agent/greenmind_agent.py" ]; then
        cp "${gateway_src}/agent/greenmind_agent.py" "${AGENT_DIR}/"
        info "Agent code installed"
    else
        warn "Agent source not found at ${gateway_src}/agent/ — skipping"
        return 0
    fi

    # Agent venv
    local agent_venv="${AGENT_DIR}/venv"
    if [ -d "${agent_venv}" ] && [ -x "${agent_venv}/bin/python" ]; then
        info "Agent venv exists, updating..."
    else
        python3 -m venv "${agent_venv}"
    fi
    "${agent_venv}/bin/pip" install --upgrade pip --quiet
    "${agent_venv}/bin/pip" install httpx psutil packaging pydantic --quiet

    # Sudoers for restricted agent privileges
    info "Configuring sudo whitelist for agent..."
    cat > "${SUDOERS_FILE}" << 'SUDOERS'
# /etc/sudoers.d/greenmind-agent
# Minimal privileges for the GreenMind update agent.
# Only allows restarting the gateway service and rebooting.
# No general root access. No shell execution.

greenmind-agent ALL=(root) NOPASSWD: /usr/bin/systemctl restart greenmind-gateway
greenmind-agent ALL=(root) NOPASSWD: /usr/bin/systemctl status greenmind-gateway
greenmind-agent ALL=(root) NOPASSWD: /usr/bin/systemctl is-active greenmind-gateway
greenmind-agent ALL=(root) NOPASSWD: /usr/sbin/reboot
SUDOERS
    chmod 0440 "${SUDOERS_FILE}"
    visudo -cf "${SUDOERS_FILE}" >/dev/null 2>&1 || die "Invalid sudoers file generated"

    success "OTA update agent configured"
}

# ── Step 7: Create Directory Structure ───────────────────────────────────────

create_directories() {
    step "7/11 — Directory Structure & Permissions"

    mkdir -p "${DATA_DIR}"
    mkdir -p "${LOG_DIR}"
    mkdir -p "${WAV_DIR}"
    mkdir -p "${CONFIG_DIR}"
    mkdir -p "${CONFIG_DIR}/versions"
    mkdir -p "${RELEASES_DIR}"
    mkdir -p "${BACKUPS_DIR}"

    # Data directory: owned by gateway user, group-readable by agent
    chown -R "${SERVICE_USER}:${SERVICE_USER}" "${DATA_DIR}"
    chmod 750 "${DATA_DIR}"

    # Secrets file: readable by gateway + agent only
    if [ ! -f "${DATA_DIR}/secrets.json" ]; then
        echo "{}" > "${DATA_DIR}/secrets.json"
    fi
    chown "${SERVICE_USER}:${SERVICE_USER}" "${DATA_DIR}/secrets.json"
    chmod 640 "${DATA_DIR}/secrets.json"
    info "secrets.json permissions hardened"

    # Config directory
    chown -R "${SERVICE_USER}:${SERVICE_USER}" "${CONFIG_DIR}"
    chmod 750 "${CONFIG_DIR}"

    # Agent directory
    chown -R "${AGENT_USER}:${AGENT_USER}" "${AGENT_DIR}"

    # Release directory: agent writes new releases, gateway reads/executes them
    chown -R "${AGENT_USER}:${SERVICE_USER}" "${RELEASES_DIR}"
    chmod -R 750 "${RELEASES_DIR}"

    # Base directory: writable by agent (to switch symlinks), readable by gateway
    chown "${AGENT_USER}:${SERVICE_USER}" "${INSTALL_BASE}"
    chmod 775 "${INSTALL_BASE}"

    # Logs writable by gateway user
    chown -R "${SERVICE_USER}:${SERVICE_USER}" "${LOG_DIR}"
    chmod 750 "${LOG_DIR}"

    # WAV directory writable by gateway user
    chown -R "${SERVICE_USER}:${SERVICE_USER}" "${WAV_DIR}"
    chmod 750 "${WAV_DIR}"

    success "Directory structure created with hardened permissions"
}

# ── Step 8: Configure Environment ────────────────────────────────────────────

configure_environment() {
    step "8/11 — Environment Configuration"

    local env_file="${CURRENT_LINK}/.env"

    if [ -f "${env_file}" ]; then
        info "Existing .env found — preserving current configuration"
        warn "To reconfigure, edit: ${env_file}"
    else
        info "Creating .env from template..."

        # Check if running interactively (stdin is a terminal)
        if [ -t 0 ]; then
            echo ""
            echo -e "${CYAN}${BOLD}Gateway Configuration${NC}"
            echo -e "${CYAN}─────────────────────${NC}"
            echo ""

            # Cloud API URL
            local default_api="https://green-mind.ch/api/v1"
            echo -e "Cloud API URL ${YELLOW}[${default_api}]${NC}:"
            read -r api_url
            api_url="${api_url:-${default_api}}"

            echo ""
            echo -e "${YELLOW}Note: The gateway will register via the Setup Portal (WiFi AP)."
            echo -e "No API key is needed at install time — it will be obtained"
            echo -e "automatically during the pairing process.${NC}"
            echo ""
        else
            # Non-interactive mode (piped via curl)
            warn "Non-interactive mode detected (curl pipe)."
            warn "Using default configuration. Edit ${env_file} after install."
            local api_url="https://green-mind.ch/api/v1"
        fi

        cat > "${env_file}" << ENV
# GreenMind Gateway — Environment Variables
# Generated by install-gateway.sh v${SCRIPT_VERSION} on $(date -Iseconds)
#
# ⚠️  Do NOT commit this file. It contains deployment-specific configuration.

# Cloud backend URL (without trailing slash)
CLOUD_API_URL=${api_url}

# Firmware OTA sync URL (same backend, separate setting for flexibility)
FIRMWARE_API_URL=${api_url}

# Local persistence paths
DB_PATH=${DATA_DIR}/queue.db
SECRETS_PATH=${DATA_DIR}/secrets.json
OTA_DB_PATH=${DATA_DIR}/ota.db
FIRMWARE_DIR=${DATA_DIR}/firmware

# Logging
LOG_DIR=${LOG_DIR}
LOG_LEVEL=INFO

# Upload intervals (seconds)
UPLOAD_INTERVAL=10
HEARTBEAT_INTERVAL=60

# Queue limits
MAX_QUEUE_SIZE=100000

# WAV archival
WAV_DIR=${WAV_DIR}
WAV_CHUNK_MINUTES=10
ENV

        # Harden .env permissions (readable only by root + gateway user)
        chown root:"${SERVICE_USER}" "${env_file}"
        chmod 640 "${env_file}"

        success ".env created and secured (chmod 640)"
    fi
}

# ── Step 9: Install systemd Services ─────────────────────────────────────────

install_services() {
    step "9/11 — systemd Services"

    local gateway_src="${REPO_DIR}/greenmind-gateway"

    # Gateway service
    info "Installing ${GATEWAY_SERVICE}.service..."
    cat > "/etc/systemd/system/${GATEWAY_SERVICE}.service" << 'SERVICE'
[Unit]
Description=GreenMind Raspberry Pi Edge Gateway
After=network-online.target NetworkManager.service
Wants=network-online.target
StartLimitIntervalSec=60
StartLimitBurst=5

[Service]
Type=simple
# Root is required for nmcli AP management without Polkit configuration.
User=root
WorkingDirectory=/opt/greenmind/current
EnvironmentFile=-/opt/greenmind/current/.env
ExecStart=/opt/greenmind/current/venv/bin/python -m src.main

# Restart policy with crash-loop protection
Restart=always
RestartSec=5

# Logging
StandardOutput=journal
StandardError=journal
SyslogIdentifier=greenmind-gateway

[Install]
WantedBy=multi-user.target
SERVICE

    # Agent service
    info "Installing ${AGENT_SERVICE}.service..."
    cat > "/etc/systemd/system/${AGENT_SERVICE}.service" << 'SERVICE'
[Unit]
Description=GreenMind Update Agent
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=60
StartLimitBurst=5

[Service]
Type=simple
User=greenmind-agent
Group=greenmind-agent
WorkingDirectory=/opt/greenmind/agent
ExecStart=/opt/greenmind/agent/venv/bin/python greenmind_agent.py

# Restart policy
Restart=always
RestartSec=5

# Logging
StandardOutput=journal
StandardError=journal
SyslogIdentifier=greenmind-agent

# Hardening — agent needs write to /opt/greenmind/* and /tmp for downloads
NoNewPrivileges=false
ProtectSystem=strict
ReadWritePaths=/opt/greenmind /tmp
ProtectHome=yes
PrivateTmp=false

[Install]
WantedBy=multi-user.target
SERVICE

    # Reload systemd
    systemctl daemon-reload

    # Enable services (they start on boot)
    systemctl enable "${GATEWAY_SERVICE}" --quiet
    systemctl enable "${AGENT_SERVICE}" --quiet

    success "systemd services installed and enabled"
}

# ── Step 10: Log Rotation ────────────────────────────────────────────────────

configure_logrotate() {
    step "10/11 — Log Rotation & OTA Cron"

    # Logrotate configuration
    info "Configuring logrotate..."
    cat > "${LOGROTATE_CONF}" << 'LOGROTATE'
/opt/greenmind/data/logs/*.log {
    daily
    rotate 14
    compress
    delaycompress
    missingok
    notifempty
    create 640 greenmind greenmind
    copytruncate
    dateext
    dateformat -%Y%m%d
    maxsize 50M
}
LOGROTATE
    chmod 644 "${LOGROTATE_CONF}"
    success "Logrotate: 14 days retention, 50 MB max, compressed"

    # OTA update cron job (daily at 03:00)
    info "Configuring OTA update cron..."
    cat > "${CRON_FILE}" << 'CRON'
# GreenMind OTA update check — daily at 03:00
# The agent normally polls every 30s, but this ensures a fresh check
# after any overnight maintenance window.
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

0 3 * * * root systemctl restart greenmind-agent 2>/dev/null || true
CRON
    chmod 644 "${CRON_FILE}"
    success "OTA cron: agent restart daily at 03:00"
}

# ── Step 11: Start Services ─────────────────────────────────────────────────

start_services() {
    step "11/11 — Starting Services"

    info "Starting ${GATEWAY_SERVICE}..."
    systemctl start "${GATEWAY_SERVICE}" || warn "Gateway service failed to start (may need pairing first)"

    info "Starting ${AGENT_SERVICE}..."
    systemctl start "${AGENT_SERVICE}" || warn "Agent service failed to start"

    # Brief pause for services to initialize
    sleep 2

    echo ""
    echo -e "${CYAN}Service Status:${NC}"
    echo "────────────────────────────────────────"

    local gw_status agent_status
    gw_status="$(systemctl is-active ${GATEWAY_SERVICE} 2>/dev/null || echo 'inactive')"
    agent_status="$(systemctl is-active ${AGENT_SERVICE} 2>/dev/null || echo 'inactive')"

    if [ "${gw_status}" = "active" ]; then
        echo -e "  ${GATEWAY_SERVICE}:  ${GREEN}● active${NC}"
    else
        echo -e "  ${GATEWAY_SERVICE}:  ${YELLOW}○ ${gw_status}${NC}"
    fi

    if [ "${agent_status}" = "active" ]; then
        echo -e "  ${AGENT_SERVICE}:   ${GREEN}● active${NC}"
    else
        echo -e "  ${AGENT_SERVICE}:   ${YELLOW}○ ${agent_status}${NC}"
    fi
    echo ""
}

# ── Summary ──────────────────────────────────────────────────────────────────

print_summary() {
    local ip_addr
    ip_addr="$(hostname -I 2>/dev/null | awk '{print $1}')" || ip_addr="unknown"
    local hostname_val
    hostname_val="$(hostname 2>/dev/null)" || hostname_val="unknown"

    echo ""
    echo -e "${GREEN}${BOLD}"
    echo "  ╔══════════════════════════════════════════════════════╗"
    echo "  ║                                                      ║"
    echo "  ║   🌿 Installation Complete!                           ║"
    echo "  ║                                                      ║"
    echo "  ╚══════════════════════════════════════════════════════╝"
    echo -e "${NC}"

    echo -e "${BOLD}System Info${NC}"
    echo "────────────────────────────────────────"
    echo -e "  Hostname:        ${hostname_val}"
    echo -e "  IP Address:      ${ip_addr}"
    echo -e "  Architecture:    $(uname -m)"
    echo -e "  Python:          $(python3 --version 2>&1)"
    echo -e "  Installer:       v${SCRIPT_VERSION}"
    echo ""

    echo -e "${BOLD}Installation Paths${NC}"
    echo "────────────────────────────────────────"
    echo -e "  Active release:  ${CURRENT_LINK}"
    echo -e "  Configuration:   ${CURRENT_LINK}/.env"
    echo -e "  Data directory:  ${DATA_DIR}"
    echo -e "  Log directory:   ${LOG_DIR}"
    echo -e "  WAV archive:     ${WAV_DIR}"
    echo -e "  Agent:           ${AGENT_DIR}"
    echo ""

    echo -e "${BOLD}Useful Commands${NC}"
    echo "────────────────────────────────────────"
    echo -e "  ${CYAN}sudo systemctl status ${GATEWAY_SERVICE}${NC}   — service status"
    echo -e "  ${CYAN}sudo journalctl -u ${GATEWAY_SERVICE} -f${NC}  — live logs"
    echo -e "  ${CYAN}sudo systemctl restart ${GATEWAY_SERVICE}${NC} — restart"
    echo -e "  ${CYAN}sudo systemctl status ${AGENT_SERVICE}${NC}    — agent status"
    echo ""

    echo -e "${BOLD}Next Steps${NC}"
    echo "────────────────────────────────────────"
    echo -e "  1. ${YELLOW}Connect to the gateway WiFi AP: GreenMind-Gateway-XXXX${NC}"
    echo -e "  2. ${YELLOW}Open http://10.42.0.1 in your browser${NC}"
    echo -e "  3. ${YELLOW}Enter your WiFi credentials and pairing code${NC}"
    echo -e "  4. ${YELLOW}The gateway will register with the cloud automatically${NC}"
    echo ""
    echo -e "  ${BLUE}Dashboard: https://green-mind.ch${NC}"
    echo -e "  ${BLUE}Docs: https://github.com/Dinten-dev/GreenMindRPIv1${NC}"
    echo ""
}

# ── Main ─────────────────────────────────────────────────────────────────────

main() {
    banner
    check_root
    check_architecture
    check_os
    check_internet
    check_disk_space

    echo ""
    echo -e "${BOLD}Starting installation...${NC}"
    echo ""

    system_update
    install_dependencies
    create_users
    clone_repository
    setup_venv
    setup_agent
    create_directories
    configure_environment
    install_services
    configure_logrotate
    start_services
    print_summary
}

main "$@"
