# Plant Wiki Gateway Architecture

## 1. Data Flow
1. ESP32 sendet Plant-Wiki-kompatible JSON-Daten an `POST /gw/ingest`.
2. Gateway validiert Schema und Datentypen.
3. Gateway normalisiert die Daten (z. B. UTC-Timestamps), bildet einen deterministischen `request_id` Hash und speichert in SQLite Queue.
4. Forwarder-Thread liest FIFO (`ORDER BY id ASC`) und sendet mit Bearer-Token an Backend (`POST /ingest`).
5. Erfolgreich zugestellte Einträge werden aus der Queue entfernt.

## 2. Offline Buffering
- Queue liegt auf Disk: `/var/lib/plantwiki-gateway/queue.db`
- SQLite im WAL-Modus mit `synchronous=FULL`
- Bei Backend-Ausfall verbleiben Einträge sicher in der Queue
- Nach Wiederverfügbarkeit des Backends werden Einträge sukzessive abgearbeitet

## 3. Retry Logic
- Fehlerklassen:
  - transient: Netzwerkfehler, Timeouts, HTTP `5xx`, `429`, `408`
  - permanent: HTTP `4xx` (außer obige)
- Transiente Fehler:
  - `attempts += 1`
  - `next_retry` mit exponentiellem Backoff
  - Zustand bleibt `queued`
- Permanente Fehler:
  - Eintrag wird `dead_letter`
  - kein weiteres Retry
- Schutz gegen Endlosschleifen:
  - `MAX_ATTEMPTS` begrenzt Retries

## 4. Failure Recovery
### Backend nicht erreichbar
- Ingest bleibt verfügbar (`202` nach Persistierung)
- Queue wächst
- Forwarder markiert `backend_status=offline`
- Nach Rückkehr: Queue drain

### Prozessabsturz
- systemd startet Dienst automatisch neu (`Restart=always`, `RestartSec=2`)
- Persistierte Queue bleibt erhalten
- Forwarder setzt Verarbeitung fort

### Systemneustart
- Service startet beim Boot (`WantedBy=multi-user.target`)
- SQLite-Daten bleiben auf Disk erhalten

## 5. Security Model
- Laufzeituser: `plantwiki` (separater Systemuser, kein Login)
- Secrets in `/etc/plantwiki-gateway/config.env` (root-only, `600`)
- systemd hardening:
  - `NoNewPrivileges=true`
  - `ProtectSystem=strict`
  - `ProtectHome=true`
  - `PrivateTmp=true`
  - reduzierte Privilegien/Kapabilitäten
- Netzwerkhärtung:
  - Firewall-Regeln auf notwendige Ports (22, 8081)
  - SSH key-only

## 6. Boot Process
1. Linux bootet in `multi-user.target`.
2. systemd startet `plantwiki-gateway.service` nach `network-online.target`.
3. Service lädt `/etc/plantwiki-gateway/config.env`.
4. Python-Prozess startet HTTP-Server und Forwarder.
5. Health-Endpoint liefert Betriebszustand.

## 7. Crash Recovery
- Wenn Python-Prozess abstürzt:
  - systemd erkennt Exit
  - automatischer Neustart nach 2 Sekunden
- Queue-Zustand bleibt erhalten
- Keine In-Memory-only Datenpfade für akzeptierte Requests

## 8. Queue Schema
```sql
queue(
  id INTEGER PRIMARY KEY,
  request_id TEXT UNIQUE,
  payload TEXT,
  created_at TIMESTAMP,
  status TEXT,
  attempts INTEGER,
  next_retry TIMESTAMP,
  last_error TEXT
)
```

Verwendete Status:
- `queued`
- `dead_letter`

## 9. Health/Observability
- `GET /gw/health`
  - `queue_depth`
  - `dead_letter_depth`
  - `last_forward_success`
  - `backend_status`
- Journald Logs:
  - ingest requests
  - retry schedule
  - dead-letter transitions
  - forward success
