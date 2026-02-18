# Plant Wiki Raspberry Pi Gateway

## Überblick
Der Plant Wiki Gateway ist ein robuster Edge-Dienst für Raspberry Pi (Bookworm, 64-bit), der Sensordaten von ESP32-Geräten lokal annimmt, persistent in SQLite puffert und zuverlässig an das Plant-Wiki-Backend (`POST /ingest`) weiterleitet.

Ziele:
- Keine Datenverluste bei Netzwerkproblemen
- Automatischer Neustart bei Absturz
- Sichere, minimalistische Laufzeitumgebung (systemd hardening + dedizierter User)
- Einfache Installation und Updates

## Architektur (ASCII)
```text
+------------------+      WiFi       +-----------------------------+
| ESP32 Sensors    | --------------> | Raspberry Pi Gateway        |
| (Plant payloads) |                 | 0.0.0.0:8081               |
+------------------+                 |  POST /gw/ingest            |
                                     |  GET  /gw/health            |
                                     +-------------+---------------+
                                                   |
                                                   v
                                     +-----------------------------+
                                     | SQLite Queue (WAL)          |
                                     | /var/lib/plantwiki-gateway/ |
                                     | queue.db                    |
                                     +-------------+---------------+
                                                   |
                                                   v
                                     +-----------------------------+
                                     | Forwarder Worker            |
                                     | Retry + Exponential Backoff |
                                     | Dead-letter on permanent    |
                                     +-------------+---------------+
                                                   |
                                                   v
                                     +-----------------------------+
                                     | Plant Wiki Backend          |
                                     | POST /ingest                |
                                     | Authorization: Bearer key   |
                                     +-----------------------------+
```

## Architekturfluss
`ESP32 -> Gateway -> SQLite Queue -> Forwarder -> Backend`

Ablauf:
1. ESP32 sendet Messdaten an `POST /gw/ingest`.
2. Gateway validiert Payload und erzeugt deterministische `request_id` (SHA-256 über normalisierte JSON-Payload).
3. Payload wird crash-sicher in SQLite (WAL, FULL sync) gespeichert.
4. Background-Forwarder verarbeitet FIFO und sendet an Backend.
5. Bei Fehlern greift Retry-Logik mit Exponential Backoff.
6. Permanente Fehler (bestimmte 4xx) landen als Dead-letter.

## Komponenten
- `gateway/main.py`:
  - HTTP Server (`ThreadingHTTPServer`)
  - Endpunkte `/gw/ingest`, `/gw/health`
  - Validierung und Queue-Enqueue
- `gateway/queue.py`:
  - SQLite Queue Store
  - WAL + FULL sync
  - FIFO-Auswahl über `id ASC`
  - Dedupe über `request_id UNIQUE`
- `gateway/forwarder.py`:
  - Hintergrund-Worker
  - Backend-POST mit Bearer Token
  - Exponential Backoff + Dead-letter
- `systemd/plantwiki-gateway.service`:
  - Auto-start beim Boot
  - Restart bei Crash
  - systemd hardening-Optionen

## API
### `POST /gw/ingest`
Erwartetes Format:
```json
{
  "device_id": "uuid",
  "samples": [
    {
      "timestamp": "2026-02-06T12:00:00Z",
      "metric_key": "air_temperature_c",
      "species_name": "Tomate",
      "value": 23.4
    }
  ]
}
```

Antwort:
- `202 Accepted` bei erfolgreicher Persistierung
- enthält `request_id` und `deduplicated`

### `GET /gw/health`
Beispiel:
```json
{
  "queue_depth": 120,
  "dead_letter_depth": 2,
  "last_forward_success": "2026-02-17T15:42:00Z",
  "backend_status": "online"
}
```

## Sicherheit
### Secrets Handling
- Runtime-Konfiguration in `/etc/plantwiki-gateway/config.env`
- Datei-Rechte: `root:root`, `chmod 600`
- API-Key liegt nicht im Code und nicht im Git-Repository

### User Separation
- Service läuft als dedizierter Systemuser `plantwiki`
- Kein Login-Shell-Account (`/usr/sbin/nologin`)

### systemd Hardening
Aktiv in der Unit:
- `NoNewPrivileges=true`
- `ProtectSystem=strict`
- `ProtectHome=true`
- `PrivateTmp=true`
- zusätzliche Kernel-/Capability-Restriktionen

### Firewall
Empfohlene Regeln (via `ufw`):
- allow `22/tcp`
- allow `8081/tcp`
- deny incoming default

### SSH
Empfohlen:
- `PasswordAuthentication no`
- `PubkeyAuthentication yes`
- nur Key-basierter Zugriff

## Logging
- Logs gehen an `journald`
- Live-Logs:
```bash
journalctl -u plantwiki-gateway -f
```
- Loglevel über `LOG_LEVEL` in `config.env` (`DEBUG`, `INFO`, `WARNING`, `ERROR`)

## Monitoring
- Gesundheitscheck:
```bash
curl http://127.0.0.1:8081/gw/health
```
- Wichtige Indikatoren:
  - `queue_depth` steigt bei Backend-Ausfall
  - `backend_status` (`online`/`offline`)
  - `last_forward_success`

## Repository Struktur
```text
plantwiki-gateway/
  README.md
  DEPLOYMENT.md
  ARCHITECTURE.md
  gateway/
    __init__.py
    main.py
    queue.py
    forwarder.py
    config.py
  systemd/
    plantwiki-gateway.service
  scripts/
    install.sh
    update.sh
    uninstall.sh
  config/
    config.env.example
  tests/
    simulate_esp32.py
```

## Schnellstart (lokal)
```bash
cp config/config.env.example /tmp/plantwiki-config.env
# DEVICE_API_KEY und BACKEND_URL in /tmp/plantwiki-config.env anpassen
PLANTWIKI_CONFIG_FILE=/tmp/plantwiki-config.env python3 -m gateway.main
```
