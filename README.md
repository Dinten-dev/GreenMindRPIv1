# GreenMind Raspberry Pi Gateway

Gateway software for Raspberry Pi OS Lite Bookworm, 64-bit. It receives sensor batches,
creates WAV recordings, buffers measurements in SQLite and uploads acknowledged data to the cloud.
Sensor firmware lives in GreenMindArdu; this repository contains the Pi application.

## Download and install


| Component | Download | Installation guide |
| --- | --- | --- |
| Direct-to-Cloud sensor, Production v2.8 | [Firmware ZIP](https://github.com/Dinten-dev/GreenMindArdu/releases/download/manual-2026-10-10/direct-production-2.8.zip) | [Direct USB installation](https://github.com/Dinten-dev/GreenMindArdu/blob/main/docs/direct-to-cloud.md) |
| Direct-to-Gateway sensor v1.2.0 | [Firmware ZIP](https://github.com/Dinten-dev/GreenMindArdu/releases/download/manual-2026-10-10/gateway-sensor-1.2.0.zip) | [Gateway sensor USB installation](https://github.com/Dinten-dev/GreenMindArdu/blob/main/docs/gateway-sensor.md) |
| Raspberry Pi Gateway, manual maintenance candidate | [Installer-free maintenance ZIP](https://github.com/Dinten-dev/GreenMindRPIv1/releases/download/manual-2026-10-10/gateway-maintenance-2026-10-10.zip) | [Gateway installation and maintenance](https://github.com/Dinten-dev/GreenMindRPIv1/blob/main/docs/gateway-installation.md) |
| Cloud backend and dashboard, reviewed source snapshot | [Source archive](https://github.com/Dinten-dev/GreenMindDB/releases/download/source-2026-10-10/GreenMindDB-source-2026-10-10.tar.gz) | [Cloud setup](https://github.com/Dinten-dev/GreenMindDB/blob/main/README.md#local-development) and [backend guide](https://github.com/Dinten-dev/GreenMindDB/blob/main/backend/README.md) |


[All downloads and checksums](docs/downloads.md) · [Installation and maintenance guide](docs/gateway-installation.md).
The 10 October maintenance candidate adds WAV completeness diagnostics and fixes TLS/WebSocket setup.
It has automated coverage; installation on Peter's actual Pi and field hardware acceptance remain pending.

## Setup and maintenance

Register the Gateway using its local setup page and a Gateway dashboard code.
Then register each sensor through its own hotspot/web form using a Gateway sensor code.
The obsolete BLE provisioning worker is no longer started.

For an existing Pi, review its installed revision, preserve queued WAVs, SQLite and configuration,
and schedule a maintenance window. The public maintenance ZIP contains no installer or update agent.
The general `install-gateway.sh` installs and enables the remote update agent and starts services;
it is a separate new-installation procedure, not a field maintenance shortcut.

## Repository layout

| Directory | Purpose |
| --- | --- |
| `greenmind-gateway/src/` | Receiving, setup, WAV recording and upload runtime |
| `greenmind-gateway/agent/` | Existing desired-state update agent |
| `greenmind-gateway/tests/` | Gateway and agent tests |
| `greenmind-gateway/tools/` | Explicit operator utilities |
| `docs/` | Downloads, installation, maintenance and historical reports |

The `greenmind-gateway/` path is retained for installer, systemd and CI compatibility.

## Development

```bash
cd greenmind-gateway
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --require-hashes -r requirements.lock
python -m pip install -r requirements-dev.txt
python -m pytest -q
python -m ruff check .
python -m ruff format --check .
bash -n install-gateway.sh
```

[Documentation index](docs/README.md) · [Gateway runtime and installer reference](greenmind-gateway/README.md).
Short WAV warnings reveal incomplete recording; they do not prove or repair the original radio-loss cause.
Direct-only sensors require no Pi maintenance to keep uploading.
