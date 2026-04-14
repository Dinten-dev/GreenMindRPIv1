# GreenMind Raspberry Pi Gateway

> Production-ready edge gateway for the GreenMind IoT platform. Receives bioelectrical sensor data from ESP32 nodes at 380 Hz, archives raw data as WAV files, buffers aggregates locally in SQLite, and uploads to the cloud backend. Includes a **desired-state update agent** for secure over-the-air remote management.

---

## Quick Start

```bash
# 1. Flash Raspberry Pi OS Lite (Bookworm) and enable SSH
# 2. Clone and install
git clone <repo-url> /opt/greenmind
sudo bash /opt/greenmind/gateway/scripts/install.sh

# 3. Start the service
sudo systemctl start greenmind-gateway
```

The gateway will automatically enter **Setup Mode** on first boot.

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

### View logs
```bash
# Live journal
sudo journalctl -u greenmind-gateway -f

# Rotating log files
cat /opt/greenmind/data/logs/gateway.log
```

---

## Project Structure

```
greenmind-gateway/
├── .env                    # Runtime configuration
├── .env.example            # Template (safe to commit)
├── requirements.txt        # Python dependencies
├── agent/                  # OTA Update Agent
│   ├── greenmind_agent.py  # Main agent (~700 lines)
│   └── tests/
│       └── test_agent.py   # Agent unit tests (14 tests)
├── scripts/
│   ├── install.sh          # Gateway automated installer
│   ├── deploy_remote.sh    # Remote deploy (SSH/SCP)
│   └── deploy_agent.sh     # Agent deploy (expect-based)
├── systemd/
│   ├── greenmind-gateway.service  # Gateway systemd unit (symlink-based)
│   ├── greenmind-agent.service    # Agent systemd unit (User=greenmind-agent)
│   └── greenmind-agent-sudoers    # Restricted sudo whitelist
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
