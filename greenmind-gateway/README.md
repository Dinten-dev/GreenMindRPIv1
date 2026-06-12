# GreenMind Raspberry Pi Gateway

> Production-ready edge gateway for the GreenMind IoT platform. Receives bioelectrical sensor data from ESP32 nodes at 380 Hz, archives raw data as WAV files, buffers aggregates locally in SQLite, and uploads to the cloud backend. Includes a **desired-state update agent** for secure over-the-air remote management.

---

## Quick Start

### One-Liner Install

Flash **Raspberry Pi OS Lite (Bookworm, 64-bit)**, enable SSH, then run:

```bash
curl -fsSL https://raw.githubusercontent.com/Dinten-dev/GreenMindRPIv1/master/greenmind-gateway/install-gateway.sh | sudo bash
```

This single command performs the entire setup — from system updates to running services.

### What the Installer Does

| Step | Action | Details |
|------|--------|---------|
| **1** | System Update | `apt update && apt upgrade -y` (non-interactive) |
| **2** | Dependencies | python3, python3-pip, python3-venv, git, curl, jq, sqlite3, NetworkManager, logrotate |
| **3** | System Users | Creates `greenmind` (gateway) + `greenmind-agent` (OTA), both non-login |
| **4** | Clone Repository | Clones to `/opt/greenmind/repo`, creates initial release with atomic symlink |
| **5** | Python venv | Creates virtualenv, installs `requirements.txt` |
| **6** | OTA Agent | Installs agent code + venv + restricted sudoers whitelist |
| **7** | Directories | Creates data/logs/wav/config/releases/backups with hardened permissions |
| **8** | Environment | Interactive `.env` configuration (or defaults in curl-pipe mode) |
| **9** | systemd Services | Installs + enables `greenmind-gateway.service` + `greenmind-agent.service` |
| **10** | Log Rotation + Cron | logrotate (14 days, 50 MB max) + daily OTA agent restart at 03:00 |
| **11** | Start Services | Starts both services, prints colored status summary |

### After Installation

1. Connect to the WiFi access point: **GreenMind-Gateway-XXXX**
2. Open `http://10.42.0.1` in your browser
3. Enter your WiFi credentials and the 6-character pairing code from the dashboard
4. The gateway registers automatically and begins streaming sensor data

### Manual Install (Alternative)

```bash
# Clone and install manually
git clone https://github.com/Dinten-dev/GreenMindRPIv1.git /opt/greenmind/repo
sudo bash /opt/greenmind/repo/greenmind-gateway/install-gateway.sh
```

> **Note:** The installer is **idempotent** — it can be run multiple times safely. Re-running will pull the latest code, update dependencies, and restart services without data loss.

### Prerequisites

- **Hardware:** Raspberry Pi 4/5 (or Zero 2 W) with ARM64
- **OS:** Raspberry Pi OS Lite — Debian Bookworm, 64-bit
- **Network:** Internet connection for initial setup
- **Disk:** ≥ 500 MB free on `/opt`

> ⚠️ The installer checks for ARM architecture and warns on non-Pi systems.

### ⚡ Stromversorgung der Sensoren (WICHTIG)

> **Jeder ESP32-Sensor MUSS über ein eigenes USB-Netzteil versorgt werden.**

Die Sensoren dürfen **nicht** an den USB-Ports des Raspberry Pi angeschlossen werden. Durch die gemeinsame Masse (Ground) entsteht eine 50-Hz-Masseschleife mit der Netzstromversorgung, die den AD8232-Verstärker in Sättigung treibt (Railing). Das Biosignal wird dadurch vollständig mit Netzbrumm überlagert und ist nicht verwertbar.

```
✅ RICHTIG                          ❌ FALSCH
                                    
Steckdose ─── Netzteil A ─── RPi    Steckdose ─── Netzteil ─── RPi
Steckdose ─── Netzteil B ─── ESP32              └── USB ──── ESP32
                                                    ↑ 50 Hz Masseschleife!
```

**Empfohlene Netzteile:**
- Sensor: USB 5V / 500 mA (beliebiger USB-Adapter)
- Raspberry Pi: offizielles RPi-Netzteil (5V / 3A)

> Dieses Problem wurde im Pilotbetrieb (Gloor, Juni 2026) identifiziert. Die neue Firmware v1.0.2 enthält zusätzlich einen digitalen 50-Hz-Notchfilter als Absicherung, die physische Trennung der Stromversorgung bleibt aber zwingend.

---

## Architecture

```
Boot → is provisioned?
        ├── NO  → Start AP (GreenMind-Gateway-XXXX) → Setup Portal on :80
        └── YES → Runtime Mode
                   ├── FastAPI Ingest Server (:80)
                   │    └── WAV Writer (10-min chunks, 16-bit PCM, 380 Hz)
                   ├── Upload Worker (aggregate readings → Cloud)
                   ├── WAV Uploader (completed WAV → Cloud MinIO)
                   ├── Heartbeat Worker (60s → Cloud telemetry)
                   └── Remote Manager (command polling)

Update Agent (separate systemd service, runs as greenmind-agent user):
    └── Poll Cloud → Compare Desired State → Download → Verify → Apply → Healthcheck → Report
```

---

## Pairing Guide

### 1. Gateway First Boot
The gateway creates a WiFi access point: **GreenMind-Gateway-XXXX** (last 4 chars of hardware serial).

### 2. Connect with Phone
Connect your phone to the AP and open `http://10.42.0.1` in a browser.

### 3. Setup Form
Enter:
- **WiFi SSID** — your greenhouse network
- **WiFi Password**
- **Pairing Code** — 6-character code from the cloud dashboard
- **Gateway Name** (optional)

### 4. Cloud Registration
The gateway connects to WiFi, sends `POST /api/v1/gateways/register` with the pairing code and its hardware serial, and receives an API key. Credentials are stored securely in `/opt/greenmind/data/secrets.json` (chmod 600).

### 5. Runtime Mode
The gateway reboots into runtime mode, starts accepting ESP32 sensor data, and uploads readings to the cloud.

---

## API Reference

### Local Endpoints (ESP32 → Gateway)

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/v1/ingest` | Receive sensor data (380 samples/batch, JSON) |
| GET | `/api/v1/health` | Local health check |

### Cloud Endpoints (Gateway → Cloud)

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| POST | `/api/v1/gateways/register` | Pairing Code | Register gateway |
| POST | `/api/v1/gateways/heartbeat` | X-Api-Key | Send health telemetry |
| POST | `/api/v1/ingest` | X-Api-Key | Upload aggregate readings (1 Hz) |
| POST | `/api/v1/wav/upload` | X-Api-Key | Upload completed WAV file (multipart) |
| GET | `/api/v1/gateways/{id}/commands` | X-Api-Key | Poll remote commands |

### Agent → Cloud Endpoints (OTA)

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| GET | `/api/v1/gateway/desired-state` | X-Api-Key | Poll desired app/config/agent version |
| POST | `/api/v1/gateway/state-report` | X-Api-Key | Report app version, disk, health, status |
| POST | `/api/v1/gateway/command-result` | X-Api-Key | Report command execution result |
| GET | `/api/v1/gateway/releases/{id}/download` | X-Api-Key | Download release tarball |
| GET | `/api/v1/gateway/configs/{id}/download` | X-Api-Key | Download config JSON |

---

## Error Codes

| Code | Description | Resolution |
|------|-------------|------------|
| **E-101** | WiFi connection failed | Check SSID and password, ensure router is in range |
| **E-202** | Cloud authentication rejected | Verify pairing code is valid and not expired (10 min TTL) |
| **E-303** | Sensor discovery timeout | Ensure ESP32 sensors are powered and broadcasting |

---

## Remote Management

GreenMind gateways are managed remotely via a **desired-state agent** that runs as a separate systemd service.

### Update Agent

The agent (`greenmind-agent.service`) polls the cloud every 30 seconds, compares the current state with the desired state, and applies updates.

#### Supported Operations
| Command | Description |
|---------|-------------|
| `restart_gateway_service` | Restart the gateway service |
| `reload_gateway_config` | Reload configuration |
| `enable_maintenance_mode` | Pause updates and data collection |
| `disable_maintenance_mode` | Resume normal operation |
| `controlled_reboot` | Controlled system reboot (requires update window) |

#### OTA Update Flow
1. Admin uploads a release tarball to the cloud
2. Admin starts a **staged rollout** (canary → early → stable)
3. Agent downloads the tarball to `/tmp/greenmind_release_*`
4. Agent verifies **SHA256** hash and optional **Ed25519 signature**
5. Agent extracts to `/opt/greenmind/releases/<version>/`
6. Agent creates venv and installs from **bundled wheels** (offline, no PyPI)
7. **Atomic symlink switch**: `/opt/greenmind/current` → new release
8. Agent restarts `greenmind-gateway.service`
9. Agent runs **6-point healthcheck** (process, HTTP, config, disk, symlink)
10. On failure → **automatic rollback** to previous release

#### Security Model
| Feature | Implementation |
|---------|----------------|
| **Privilege separation** | Agent runs as `greenmind-agent` user (non-root) |
| **Sudo whitelist** | Only `systemctl restart/reboot`, via `/etc/sudoers.d/greenmind-agent` |
| **Artifact integrity** | SHA256 verification on every download |
| **Code signing** | Ed25519 signature verification (optional, enforcement-ready) |
| **Offline install** | `pip install --no-index --find-links ./wheels` |
| **Atomic updates** | Symlink-based release switch |
| **Disk pre-check** | Requires `file_size * 2 + 100 MB` free |
| **Concurrency lock** | Global `fcntl.flock()` prevents parallel operations |
| **Update windows** | Configurable per-gateway (download anytime, apply in window) |
| **Path traversal protection** | Tarball members validated before extraction |

#### Healthcheck Suite
The agent runs 5 checks after every update:
1. **Process**: `systemctl is-active greenmind-gateway` = `active`
2. **HTTP API**: `GET http://localhost/api/v1/health` returns 200
3. **Config valid**: `/opt/greenmind/config/active.json` exists and parses as JSON
4. **Disk**: > 100 MB free
5. **Symlink**: `/opt/greenmind/current` points to an existing directory

### Reboot
The cloud can send a `controlled_reboot` command. The agent validates the update window and executes `sudo reboot`.

### Factory Reset
On the Raspberry Pi:
```bash
sudo touch /boot/reset_greenmind.txt
sudo reboot
```
This wipes all credentials and WiFi profiles, returning the gateway to Setup Mode.

---

## Heartbeat Telemetry

Every 60 seconds, the gateway sends:
- `hardware_id` — Pi serial number
- `local_ip` — current LAN IP
- `cpu_temp_c` — CPU temperature (°C)
- `ram_usage_pct` — RAM usage (%)
- `wifi_rssi_dbm` — WiFi signal strength (dBm)
- `queue_depth` — pending uploads in local SQLite

---

## Offline Resilience

When the cloud is unreachable:
1. Aggregate readings are stored in the local SQLite queue (`/opt/greenmind/data/queue.db`)
2. WAV files remain in `/opt/greenmind/data/wav/` until upload succeeds
3. The upload worker retries with exponential backoff (10s → 300s)
4. After 20 failed retries, jobs move to the Dead Letter Queue
5. Queue capacity: 100,000 entries (configurable via `MAX_QUEUE_SIZE`)

---

## WAV Archival

The gateway archives raw high-frequency sensor data as WAV files for later model training and analysis.

### Format
| Property | Value |
|----------|-------|
| **Sample Rate** | 380 Hz |
| **Bit Depth** | 16-bit signed integer |
| **Channels** | Mono |
| **Chunk Duration** | 10 minutes |
| **File Size** | ~456 KB per chunk |
| **Value Mapping** | 0–3300 mV → 0–32767 int16 |

### Storage
```
/opt/greenmind/data/wav/
└── AABBCCDDEEFF/               # Sensor MAC (no colons)
    ├── AABBCCDDEEFF_20260403T120000.wav
    ├── AABBCCDDEEFF_20260403T121000.wav
    └── ...
```

### Storage Calculation
| Timeframe | Per Sensor | 5 Sensors |
|-----------|-----------|----------|
| 1 day | 65.7 MB | 328 MB |
| 1 week | 460 MB | 2.3 GB |
| 1 month | 1.97 GB | 9.9 GB |

### Upload Flow
1. The `wav_writer` appends samples to the current 10-minute chunk
2. When the chunk is full, it closes and opens a new file
3. The `wav_uploader` worker scans for completed files every 30s
4. Completed files are uploaded via `POST /api/v1/wav/upload` (multipart)
5. On successful upload, the local file is deleted

---

## Environment Variables

All configuration is via the `.env` file at `/opt/greenmind/current/.env` (created by the installer):

| Variable | Description | Default |
|----------|-------------|---------|
| `CLOUD_API_URL` | Cloud backend URL (without trailing slash) | `https://green-mind.ch/api/v1` |
| `DB_PATH` | SQLite upload queue path | `/opt/greenmind/data/queue.db` |
| `SECRETS_PATH` | Gateway credentials (auto-generated during pairing) | `/opt/greenmind/data/secrets.json` |
| `OTA_DB_PATH` | OTA state database | `/opt/greenmind/data/ota.db` |
| `FIRMWARE_DIR` | Local firmware storage | `/opt/greenmind/data/firmware` |
| `LOG_DIR` | Log file directory | `/opt/greenmind/data/logs` |
| `LOG_LEVEL` | Logging verbosity | `INFO` |
| `UPLOAD_INTERVAL` | Cloud upload interval (seconds) | `10` |
| `HEARTBEAT_INTERVAL` | Health telemetry interval (seconds) | `60` |
| `MAX_QUEUE_SIZE` | Maximum queued uploads before dropping | `100000` |
| `WAV_DIR` | WAV archive directory | `/opt/greenmind/data/wav` |
| `WAV_CHUNK_MINUTES` | WAV file chunk duration (minutes) | `10` |

> 🔒 The `.env` file is secured with `chmod 640 root:greenmind` — only root and the gateway user can read it. **Never commit `.env` files with real credentials.**

---

## Security

### System Users & Permissions

| User | Purpose | Privileges |
|------|---------|------------|
| `greenmind` | Gateway service (data ingestion, WAV, upload) | Non-login system user, owns `/opt/greenmind/data` |
| `greenmind-agent` | OTA update agent | Non-login, restricted sudo (see below) |
| `root` | Gateway service execution | Required for `nmcli` AP management |

### Restricted Sudo (Agent)

The OTA agent has a minimal sudoers whitelist (`/etc/sudoers.d/greenmind-agent`):

```
greenmind-agent ALL=(root) NOPASSWD: /usr/bin/systemctl restart greenmind-gateway
greenmind-agent ALL=(root) NOPASSWD: /usr/bin/systemctl status greenmind-gateway
greenmind-agent ALL=(root) NOPASSWD: /usr/bin/systemctl is-active greenmind-gateway
greenmind-agent ALL=(root) NOPASSWD: /usr/sbin/reboot
```

No shell access. No general root privileges.

### File Permissions

| Path | Permissions | Owner |
|------|-------------|-------|
| `/opt/greenmind/current/.env` | `640` | `root:greenmind` |
| `/opt/greenmind/data/secrets.json` | `640` | `root:greenmind` |
| `/opt/greenmind/data/` | `750` | `greenmind:greenmind` |
| `/opt/greenmind/data/logs/` | `750` | `greenmind:greenmind` |
| `/etc/sudoers.d/greenmind-agent` | `440` | `root:root` |

### Log Rotation

Configured via `/etc/logrotate.d/greenmind-gateway`:
- **Retention:** 14 days
- **Max size:** 50 MB per file
- **Compression:** gzip (delayed)
- **Permissions:** `640 greenmind:greenmind`

---

## Troubleshooting

### Gateway stuck in Setup Mode
- Verify the AP is broadcasting: `nmcli device wifi list`
- Access setup portal at `http://10.42.0.1`

### WiFi connection fails (E-101)
- Ensure the SSID is 2.4 GHz (Pi Zero W doesn't support 5 GHz)
- Check password is correct
- Verify router allows new connections

### Pairing code rejected (E-202)
- Codes expire after 10 minutes — generate a new one
- Codes are single-use — don't reuse
- Check that the cloud backend is reachable

### Data not appearing in dashboard
- Check queue depth: `curl http://localhost/api/v1/health`
- Verify heartbeat: `sudo journalctl -u greenmind-gateway | grep heartbeat`
- Check upload worker logs for errors

### Installation issues
- Re-run the installer (idempotent): `sudo bash /opt/greenmind/repo/greenmind-gateway/install-gateway.sh`
- Check disk space: `df -h /opt`
- Verify Python version: `python3 --version` (requires 3.11+)

### Service management
```bash
# Service status
sudo systemctl status greenmind-gateway
sudo systemctl status greenmind-agent

# Live journal logs
sudo journalctl -u greenmind-gateway -f
sudo journalctl -u greenmind-agent -f

# Rotating log files
cat /opt/greenmind/data/logs/gateway.log

# Restart services
sudo systemctl restart greenmind-gateway
sudo systemctl restart greenmind-agent
```

---

## Project Structure

```
greenmind-gateway/
├── install-gateway.sh      # 🚀 One-liner production installer (curl-pipe-bash)
├── .env.example            # Template (safe to commit)
├── requirements.txt        # Python dependencies
├── agent/                  # OTA Update Agent
│   ├── greenmind_agent.py  # Main agent (~700 lines)
│   └── tests/
│       └── test_agent.py   # Agent unit tests (14 tests)
├── scripts/
│   ├── install.sh          # Legacy installer (use install-gateway.sh instead)
│   ├── deploy_remote.sh    # Remote deploy (SSH/SCP)
│   └── update.sh           # Manual code update
├── systemd/
│   ├── greenmind-gateway.service  # Gateway systemd unit (symlink-based)
│   ├── greenmind-agent.service    # Agent systemd unit (User=greenmind-agent)
│   └── greenmind-agent-sudoers    # Restricted sudo whitelist
├── config/
│   └── config.env.example  # Legacy config template
├── docs/
│   └── RPI_DEPLOYMENT.md   # Detailed deployment guide
└── src/
    ├── main.py             # Boot loader (setup vs runtime)
    ├── config.py           # Pydantic settings
    ├── core/
    │   ├── config_store.py # Secrets manager (chmod 600)
    │   ├── errors.py       # Error codes (E-101, E-202, E-303)
    │   └── logging_config.py # Rotating logs + redaction
    ├── network/
    │   └── wifi_manager.py # Async nmcli wrapper
    ├── persistence/
    │   ├── database.py     # SQLite + WAL mode
    │   └── models.py       # IngestJob + DeadLetterJob
    ├── setup_portal/
    │   ├── server.py       # Setup web app
    │   └── templates/
    │       └── setup.html  # Tailwind CDN UI
    └── runtime/
        ├── gateway_app.py  # FastAPI + async tasks
        ├── ingest_api.py   # ESP32 ingestion + WAV write + aggregate
        ├── upload_worker.py # Cloud uploader (DLQ, backoff)
        ├── wav_writer.py   # 10-min WAV chunk writer (16-bit PCM)
        ├── wav_uploader.py # Completed WAV → Cloud MinIO
        ├── heartbeat.py    # Health telemetry
        └── remote_manager.py # Remote commands
```

### Directory Layout on Raspberry Pi

```
/opt/greenmind/
├── current → releases/1.2.0  # Atomic symlink to active release
├── releases/                 # Release versions (keep last 3)
│   ├── 1.0.0/
│   ├── 1.1.0/
│   └── 1.2.0/
│       ├── src/
│       ├── wheels/           # Pre-built Python wheels
│       ├── requirements.lock
│       ├── venv/             # Per-release virtualenv
│       └── .release_meta.json
├── agent/
│   ├── greenmind_agent.py    # Update agent
│   ├── venv/                 # Agent virtualenv
│   └── state.json            # Agent state persistence
├── config/
│   ├── active.json → versions/v3.json  # Atomic config symlink
│   └── versions/
│       ├── v1.json
│       ├── v2.json
│       └── v3.json
├── backups/
│   └── last_good_config.json
├── data/
│   ├── secrets.json          # Gateway credentials (640 root:greenmind-agent)
│   ├── queue.db              # SQLite upload queue
│   ├── logs/
│   └── wav/                  # Pending WAV uploads
└── gateway/                  # Legacy (pre-OTA) install path
```

---

## License

MIT
