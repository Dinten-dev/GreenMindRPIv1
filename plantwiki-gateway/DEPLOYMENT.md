# Deployment Guide (Raspberry Pi 4/5, Bookworm 64-bit)

## 1. Raspberry Pi vorbereiten
1. Raspberry Pi OS 64-bit (Bookworm) auf SD-Karte flashen.
2. SSH aktivieren.
3. Einloggen:
```bash
ssh pi@raspberrypi.local
```

## 2. System aktualisieren
```bash
sudo apt update
sudo apt upgrade -y
```

Optional empfohlen:
```bash
sudo apt install -y ufw curl
```

## 3. Repository kopieren
```bash
git clone <REPO_URL> plantwiki-gateway
cd plantwiki-gateway
```

## 4. Installieren
```bash
sudo ./scripts/install.sh
```

Das Script erledigt:
- Systemuser `plantwiki` anlegen
- Verzeichnisse erstellen:
  - `/opt/plantwiki-gateway`
  - `/etc/plantwiki-gateway`
  - `/var/lib/plantwiki-gateway`
- Config-Datei erstellen (wenn nicht vorhanden):
  - `/etc/plantwiki-gateway/config.env`
- Service installieren:
  - `/etc/systemd/system/plantwiki-gateway.service`
- Service aktivieren (`systemctl enable`)
- Falls `ufw` vorhanden:
  - default deny incoming
  - allow `22/tcp`
  - allow `8081/tcp`

## 5. Konfiguration setzen
Datei bearbeiten:
```bash
sudo nano /etc/plantwiki-gateway/config.env
```

Mindestwerte:
```env
BACKEND_URL=http://<BACKEND_HOST>:8000/ingest
DEVICE_API_KEY=<DEVICE_API_KEY>
LISTEN_ADDR=0.0.0.0:8081
MAX_QUEUE_ROWS=100000
LOG_LEVEL=INFO
```

Dateirechte prüfen:
```bash
sudo chown root:root /etc/plantwiki-gateway/config.env
sudo chmod 600 /etc/plantwiki-gateway/config.env
```

## 6. Service starten
```bash
sudo systemctl start plantwiki-gateway
```

## 7. Status prüfen
```bash
sudo systemctl status plantwiki-gateway
```

## 8. Logs prüfen
```bash
journalctl -u plantwiki-gateway -f
```

## 9. Health prüfen
```bash
curl http://127.0.0.1:8081/gw/health
```

## 10. Funktionstest mit ESP32-Simulator
```bash
python3 tests/simulate_esp32.py --gateway-url http://127.0.0.1:8081 --samples 100
```

Offline-Szenario:
1. Backend stoppen (oder Netzwerkweg blockieren).
2. Test starten:
```bash
python3 tests/simulate_esp32.py --gateway-url http://127.0.0.1:8081 --samples 100 --offline-scenario
```
3. Backend wieder starten und Drain beobachten.

## Update
```bash
sudo ./scripts/update.sh
```

## Uninstall
Standard (Config + Daten behalten):
```bash
sudo ./scripts/uninstall.sh
```

Komplett inkl. Queue/Config/User entfernen:
```bash
sudo ./scripts/uninstall.sh --purge
```

## SSH Hardening (Pflichtempfehlung)
Datei bearbeiten:
```bash
sudo nano /etc/ssh/sshd_config
```

Sicherstellen:
```text
PasswordAuthentication no
PubkeyAuthentication yes
```

Dann:
```bash
sudo systemctl restart ssh
```
