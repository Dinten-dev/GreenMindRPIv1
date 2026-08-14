# GreenMind Gateway RPi Deployment Guide

This guide details how to deploy the GreenMind Gateway on a Raspberry Pi running Raspberry Pi OS.

## Prerequisites

- Raspberry Pi (3, 4, or Zero 2 W) with Raspberry Pi OS (Bullseye/Bookworm).
- Network connection to `macmini.local` or known IP.
- SSH access to Raspberry Pi.

## Installation Steps

1. **Transfer Files**
   Copy the `greenmind-gateway` directory to the Raspberry Pi.
   ```bash
   # From your Mac
   scp -r greenmind-gateway pi@raspberrypi.local:~
   ```

2. **Run Installer**
   Connect to the Pi and run the installation script.
   ```bash
   ssh pi@raspberrypi.local
   cd greenmind-gateway
   sudo ./scripts/install.sh
   ```

3. **Configure**
   Edit the configuration file to set the API Key and Backend URL.
   ```bash
   sudo nano /etc/greenmind-gateway/config.env
   ```
   **Important Settings:**
   - `BACKEND_BASE_URL`: URL of the Mac mini API (e.g., `http://macmini.local:8000`).
   - `DEVICE_API_KEY`: Authentication token for the Mac mini API.

4. **Start Service**
   ```bash
   sudo systemctl start greenmind-gateway
   ```

## Management & Monitoring

- **Check Status**
  ```bash
  sudo systemctl status greenmind-gateway
  ```

- **View Logs**
  ```bash
  journalctl -u greenmind-gateway -f
  ```

- **Check Health**
  ```bash
  curl http://localhost:8081/gw/health
  ```
  Expected output:
  ```json
  {"status": "ok", "queue_depth": 0, "backend_connected": true, ...}
  ```

## Troubleshooting

- **Backend Connection Failed**:
  - Check if `macmini.local` is pingable (`ping macmini.local`).
  - If not, use IP address in `config.env`.
  - Check `gateway.py` logs for specific error codes.

- **Queue Growing**:
  - Means backend is unreachable or returning errors.
  - Gateway will auto-retry infinitely.
  - Check Mac mini logs.

## Updates

To update the gateway code:
1. Copy new files to Pi.
2. Run update script:
   ```bash
   sudo ./scripts/update.sh
   ```
