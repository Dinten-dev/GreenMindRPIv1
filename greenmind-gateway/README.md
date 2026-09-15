# GreenMind Raspberry Pi Gateway

> Production-ready edge gateway for the GreenMind IoT platform. Receives bioelectrical sensor data from ESP32 nodes at 380 Hz, archives raw data as WAV files, buffers aggregates locally in SQLite, and uploads to the cloud backend. Includes a **desired-state update agent** for secure over-the-air remote management.

---

## Gateway / Direct parallel operation on develop

The reviewed `develop` release includes compact protocol-v3 reception and durable
Gateway buffering from `afda66c`, while retaining legacy protocol-v1 `readings`
and protocol-v2 quality metadata. Direct firmware's DUAL packets are accepted at
the same `/api/v1/ingest` endpoint and acknowledged with sequence/sample count.
Replayed boot/sequence packets return `duplicate` without another WAV write.

To verify a Pi's compatibility, query its local `/api/v1/health`: this release
adds `ingest_protocol_versions: [1, 2, 3]` and `sequence_acknowledgement: true`.
An older Gateway lacking those capabilities must be explicitly upgraded before
using the new firmware's DUAL mode. Publishing develop does not upgrade any Pi.
Resolve and review the full develop commit ID and use the pinned installer below;
never deploy an unreviewed moving branch automatically. Preserve the Pi's queued
WAVs, SQLite database and credentials during upgrades.

Old Gateway-only sensors require no firmware changes. A separate Direct sensor
population can run alongside them without upgrading every Gateway. For the same
sensor in DUAL, provision the matching existing sensor/zone and Direct identity
explicitly. The two cloud archives remain separate to avoid double counting.

## Quick Start

### Pinned, Reviewed Install

Flash **Raspberry Pi OS Lite (Bookworm, 64-bit)** and enable SSH. On a trusted
workstation, select a reviewed full commit (not a branch or tag), fetch that
exact object, inspect the installer, and then run it locally on the Pi:

```bash
REVISION=replace-with-reviewed-40-hex-commit
mkdir GreenMindRPIv1 && cd GreenMindRPIv1
git init
git remote add origin https://github.com/Dinten-dev/GreenMindRPIv1.git
git fetch --depth 1 origin "$REVISION"
git checkout --detach "$REVISION"
less greenmind-gateway/install-gateway.sh
sudo bash greenmind-gateway/install-gateway.sh "$REVISION"
```

The installer refuses mutable revisions, verifies the detached checkout, and
installs both gateway and update-agent dependencies from the committed
hash-locked requirements file. Never pipe a remote installer directly to root.

### What the Installer Does

| Step | Action | Details |
|------|--------|---------|
| **1** | Package Index | Updates APT metadata without a blanket OS upgrade |
| **2** | Dependencies | python3, python3-pip, python3-venv, git, curl, jq, sqlite3, NetworkManager, logrotate |
| **3** | System Users | Creates `greenmind` (gateway) + `greenmind-agent` (OTA), both non-login |
| **4** | Pinned Repository | Fetches the required 40-hex commit and checks it out detached |
| **5** | Python venv | Creates virtualenv, installs pinned `requirements.lock` |
| **6** | OTA Agent | Installs agent code + venv + restricted sudoers whitelist |
| **7** | Directories | Creates data/logs/wav/config/releases/backups with hardened permissions |
| **8** | Environment | Interactive `.env` configuration (or safe defaults for automation) |
| **9** | systemd Services | Installs + enables `greenmind-gateway.service` + `greenmind-agent.service` |
| **10** | Log Rotation + Cron | logrotate (14 days, 50 MB max) + daily OTA agent restart at 03:00 |
| **11** | Start Services | Starts both services, prints colored status summary |

### After Installation

1. Read the per-boot setup password on the Pi console: `sudo journalctl -u greenmind-gateway -b`
2. Connect to the WiFi access point: **GreenMind-Gateway-XXXX**
3. Open `http://10.42.0.1` in your browser
4. Enter your WiFi credentials and the 6-character pairing code from the dashboard
5. The gateway registers automatically and begins streaming sensor data

### Re-run or Select a New Reviewed Revision

```bash
sudo bash /opt/greenmind/repo/greenmind-gateway/install-gateway.sh "$REVISION"
```

The revision remains mandatory on every run. Moving to another commit is an
explicit operator decision; the installer never follows a branch or tag.

### Prerequisites

- **Hardware:** Raspberry Pi 4/5 (or Zero 2 W) with ARM64
- **OS:** Raspberry Pi OS Lite — Debian Bookworm, 64-bit
- **Network:** Internet connection for initial setup
- **Disk:** ≥ 500 MB free on `/opt`

> ⚠️ The installer checks for ARM architecture and warns on non-Pi systems.

### Local Development and Verification

```bash
cd greenmind-gateway
python3 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements.lock
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check src agent tests tools
.venv/bin/python -m ruff format --check src agent tests tools
```

The load simulator uses an isolated temporary directory and leaves no SQLite,
WAV, firmware, secret, or log artifacts in the checkout:

```bash
.venv/bin/python run_load_test.py
```

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
The gateway creates **GreenMind-Gateway-XXXX** (last 4 chars of the hardware serial) with a cryptographically random password on every process boot. Read it from the physically local console or the current-boot systemd journal; there is no shared default password.

### 2. Connect with Phone
Connect your phone to the AP and open `http://10.42.0.1` in a browser.

### 3. Setup Form
Enter:
- **WiFi SSID** — your greenhouse network
- **WiFi Password**
- **Pairing Code** — 6-character code from the cloud dashboard
- **Gateway Name** (optional)

### 4. Cloud Registration
The gateway connects to WiFi, sends `POST /api/v1/gateways/register` with the pairing code and its hardware serial, and receives an API key. Credentials are stored in `/opt/greenmind/data/secrets.json` with mode `0640`, owned by the dedicated gateway account so the update-agent group can read them.

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
4. Agent verifies **SHA256** and a mandatory **Ed25519 signature**; missing key, signature, or crypto support fails closed
5. Agent validates SemVer and extracts only bounded regular files/directories (no links, devices, traversal, or archive bombs)
6. Agent creates venv and installs from **bundled wheels** (offline, no PyPI)
7. **Atomic symlink switch**: `/opt/greenmind/current` → new release
8. Agent restarts `greenmind-gateway.service`
9. Agent runs a **5-point healthcheck** (process, HTTP, config, disk, symlink)
10. On failure → **automatic rollback** to previous release

#### Security Model
| Feature | Implementation |
|---------|----------------|
| **Privilege separation** | Agent runs as `greenmind-agent` user (non-root) |
| **Sudo whitelist** | Only `systemctl restart/reboot`, via `/etc/sudoers.d/greenmind-agent` |
| **Artifact integrity** | SHA256 verification on every download |
| **Code signing** | Mandatory Ed25519 signature; missing prerequisites fail closed |
| **Offline install** | `pip install --no-index --require-hashes --find-links ./wheels` |
| **Atomic updates** | Symlink-based release switch |
| **Disk pre-check** | Requires `file_size * 2 + 100 MB` free |
| **Concurrency lock** | Global `fcntl.flock()` prevents parallel operations |
| **Update windows** | Configurable per-gateway (download anytime, apply in window) |
| **Archive protection** | Contained staging, canonical SemVer, entry/count/size/ratio bounds; links/devices rejected |

Before enabling OTA, install the trusted Ed25519 public key as `/opt/greenmind/agent/signing_key.pub`, owned by `root:greenmind-agent` and mode `0640`. Releases must bundle `requirements.lock` and a `wheels/` directory. The agent does not contact PyPI unless an operator deliberately sets `GREENMIND_ALLOW_LEGACY_ONLINE_PIP=true` as a temporary break-glass migration measure.

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
3. The upload worker retains network, server, and gateway-auth failures in the
   queue indefinitely with capped exponential backoff (10s → 300s)
4. Only malformed local JSON and individually confirmed HTTP 422 validation
   failures move to the Dead Letter Queue
5. `MAX_QUEUE_SIZE` is an alert threshold, never a deletion trigger
6. Disk guards return HTTP 507 before accepting unretained measurements

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
    ├── AABBCCDDEEFF_20260403T120000.wav.part  # active, never uploaded
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
1. The `wav_writer` appends samples to a bounded set of `.wav.part` writers and periodically flushes/fsyncs
2. Rotation closes metadata, fsyncs, then atomically renames the chunk to `.wav`
3. The `wav_uploader` worker scans for completed files every 30s
4. Completed files are uploaded via `POST /api/v1/wav/upload` (multipart)
5. Only an explicit successful upload response acknowledges deletion; storage limits never delete unacknowledged files

---

## Environment Variables

All configuration is via `/opt/greenmind/.env` (created by the installer). See `.env.example` for the complete bounded-storage and ingress settings.

| Variable | Description | Default |
|----------|-------------|---------|
| `CLOUD_API_URL` | Cloud backend URL (without trailing slash) | `https://green-mind.ch/api/v1` |
| `FIRMWARE_API_URL` | Firmware API base URL (without trailing slash) | `https://green-mind.ch/api/v1` |
| `ALLOW_INSECURE_CLOUD_HTTP` | Local development only: allow HTTP to loopback | `false` |
| `DB_PATH` | SQLite upload queue path | `/opt/greenmind/data/queue.db` |
| `SECRETS_PATH` | Gateway credentials (auto-generated during pairing) | `/opt/greenmind/data/secrets.json` |
| `OTA_DB_PATH` | OTA state database | `/opt/greenmind/data/ota.db` |
| `FIRMWARE_DIR` | Local firmware storage | `/opt/greenmind/data/firmware` |
| `LOG_DIR` | Log file directory | `/opt/greenmind/data/logs` |
| `LOG_LEVEL` | Logging verbosity | `INFO` |
| `UPLOAD_INTERVAL` | Cloud upload interval (seconds) | `10` |
| `HEARTBEAT_INTERVAL` | Health telemetry interval (seconds) | `60` |
| `MAX_QUEUE_SIZE` | Local queue warning threshold | `1000000` |
| `MAX_REQUEST_BODY_BYTES` | Maximum local HTTP body before parsing | `262144` |
| `MAX_HTTP_CONCURRENCY` | Maximum concurrent local HTTP connections | `128` |
| `HTTP_KEEPALIVE_SECONDS` | Idle HTTP keep-alive timeout | `5` |
| `MAX_SAMPLES_PER_BATCH` | Maximum readings in one sensor batch | `760` |
| `ALLOWED_SAMPLE_RATES` | Accepted sensor rates (JSON list) | `[380]` |
| `WAV_DIR` | WAV archive directory | `/opt/greenmind/data/wav` |
| `WAV_CHUNK_MINUTES` | WAV file chunk duration (minutes) | `10` |
| `WAV_IDLE_FINALIZE_SECONDS` | Finalize inactive WAV chunks | `120` |
| `WAV_MAX_OPEN_WRITERS` | LRU-bounded active sensor writers | `64` |
| `WAV_MIN_FREE_BYTES` | Stop new archival below free-space threshold | `8589934592` |
| `WAV_MAX_PENDING_BYTES` | Retained unacknowledged WAV limit | `103079215104` |
| `ENABLE_BLE_PROVISIONING` | Start retained experimental BLE worker | `false` |
| `ENABLE_EXPERIMENTAL_BIOSIGNAL_PROXY` | Enable non-durable compatibility proxy | `false` |

> 🔒 The `.env` file is secured with `chmod 640 root:greenmind` — only root and the gateway user can read it. **Never commit `.env` files with real credentials.**

Cloud and firmware URLs require HTTPS. For isolated local development only,
`ALLOW_INSECURE_CLOUD_HTTP=true` permits `http://localhost`, a literal IPv4
address in `127.0.0.0/8`, or `http://[::1]`; it never permits plaintext traffic
to a non-loopback host.

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
| `/opt/greenmind/.env` | `640` | `root:greenmind` |
| `/opt/greenmind/agent/signing_key.pub` | `640` | `root:greenmind-agent` |
| `/opt/greenmind/data/secrets.json` | `640` | `greenmind:greenmind` |
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
- Read this boot's AP password locally: `sudo journalctl -u greenmind-gateway -b | grep 'local setup'`
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
- Re-run the installer with the reviewed commit: `sudo bash /opt/greenmind/repo/greenmind-gateway/install-gateway.sh "$REVISION"`
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
├── install-gateway.sh      # Pinned-commit production installer (run locally)
├── .env.example            # Template (safe to commit)
├── requirements.txt        # Python dependencies
├── requirements.lock       # Pinned production dependencies used by releases
├── requirements-dev.txt    # Pinned pytest + Ruff toolchain
├── agent/                  # OTA Update Agent
│   ├── greenmind_agent.py  # Main agent (~700 lines)
│   └── tests/
│       └── test_agent.py   # Agent security and lifecycle tests
├── tools/
│   ├── manage_gateway_release.py # Signed release admin CLI
│   └── wav_quality.py      # WAV diagnostic tool
├── systemd/
│   ├── greenmind-gateway.service  # Gateway systemd unit (symlink-based)
│   ├── greenmind-agent.service    # Agent systemd unit (User=greenmind-agent)
│   └── greenmind-agent-sudoers    # Restricted sudo whitelist
└── src/
    ├── main.py             # Boot loader (setup vs runtime)
    ├── config.py           # Pydantic settings
    ├── validation.py       # Strict sensor protocol models and MAC canonicalization
    ├── http_limits.py      # Pre-parser ASGI request body limit
    ├── core/
    │   ├── config_store.py # Secrets manager (mode 0640)
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
    │       └── setup.html  # Setup UI with locally vendored styling
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
├── .env                       # Shared service configuration (640)
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
│   ├── signing_key.pub       # Trusted Ed25519 public key (operator-installed)
│   └── agent_state.json      # Agent state persistence
├── config/
│   ├── active.json → versions/v3.json  # Atomic config symlink
│   └── versions/
│       ├── v1.json
│       ├── v2.json
│       └── v3.json
├── backups/
│   └── last_good_config.json
├── data/
│   ├── secrets.json          # Gateway credentials (640 greenmind:greenmind)
│   ├── queue.db              # SQLite upload queue
│   ├── logs/
│   └── wav/                  # Pending WAV uploads
```

---

## License

MIT
