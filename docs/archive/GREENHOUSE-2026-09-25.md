# Gewächshaus: Gateway-Kandidat vom 24.09.2026

[Quellpaket von GitHub herunterladen](https://github.com/Dinten-dev/GreenMindRPIv1/releases/tag/field-2026-09-25).
[Quellcode auf main](https://github.com/Dinten-dev/GreenMindRPIv1/tree/main).

Den vollen Commit aus dem Feldpaket verwenden. Dieser Kandidat wurde auf dem
Entwicklungsrechner geprüft, noch nicht auf der neuen Pi-Speicherkarte.
Bestehende Gateways wurden weder neugestartet noch aktualisiert.

## Änderungen

- Gleichzeitige Wiederholungen derselben Boot-ID/Sequenz schreiben nur einmal
  WAV-Daten. Der Prozess serialisiert Cursorprüfung, WAV-Schreibvorgang und Commit.
- Jede bestätigte WAV-Portion wird vor dem ACK geflusht und synchronisiert.
  SQLite verwendet FULL statt NORMAL; dies kostet Datenträgerleistung.
- Cloud-Reset bei HTTP 410 ist standardmäßig blockiert (`ALLOW_REMOTE_RESET=false`).
  WLAN-Konfiguration und Zugangsdaten bleiben bei Cloud-Fehlern erhalten.
- Die Ausfalltests umfassen 401/403/410/502/503, DNS, Timeout, lokale Annahme und
  anschließendes Nachsenden. Dies ist kein echter Hardware-Stromausfalltest.
- Gateway-Protokolle 1, 2 und 3 sowie vorhandene Sensoren bleiben unterstützt.

## Neuer Pi

Raspberry Pi OS Lite Bookworm, 64 Bit; zuverlässige SD-Karte oder SSD, offizielles
Netzteil. **Mehr als 8 GiB frei für die Standardreserve**: Die Annahme von WAV-Daten
stoppt unter `WAV_MIN_FREE_BYTES=8589934592`. Die alte 500-MB-Installerwarnung ist
keine ausreichende Betriebsanforderung. Empfohlenes Installationsmedium: mindestens
32 GB, nach Installation tatsächliche freie Kapazität prüfen.

Den dokumentierten `install-gateway.sh` aus dem lokal geprüften Checkout mit
vollem 40-stelligem Commit starten. Bei einem vorhandenen Pi vorher SD-Abbild
beziehungsweise `/opt/greenmind` einschließlich Queue, WAVs und Zugangsdaten sichern.
Ein vorhandener Pi darf nicht gleichzeitig Daten empfangen und unkontrolliert
neu installiert werden. Der Installer ist hier nicht ausgeführt worden.

Im Gateway-Hotspot Ziel-WLAN und **Gateway-Code** aus der neuen Zone eingeben.
Server muss `https://green-mind.ch/api/v1` sein. Auf dem lokalen Pi
`http://127.0.0.1/api/v1/health` prüfen: Status OK, Protokoll 3 und Sequenz-ACK.
Nur einen Gateway-Uvicorn-Prozess betreiben; WAV-Writer und Sperre sind pro Prozess.

## Gateway-Sensor zuordnen

BLE richtet beim Biolingo-Sensor nur WLAN ein. Danach im Dashboard einen
**Gateway-Sensor-Code** für dieselbe Zone erzeugen. Aus `greenmind-gateway`:

    .venv/bin/python -m tools.register_sensor --gateway-ip 127.0.0.1

Auf dem installierten Pi entsprechend dessen Python-Umgebung unter `/opt/greenmind`
verwenden. Das Werkzeug fragt MAC und Code lokal ab, zeigt den Code nicht an und
fordert keinen Cloud-API-Schlüssel an. Es verändert die Zuordnung erst beim
bewussten Aufruf. Ein BLE-PoP oder Direct-Code ist kein Gateway-Sensor-Code.

## Morgen abhaken

1. Richtige Firma/Zone und Benutzerrechte bestätigen; noch unbekannter Zonenname.
2. Einen Sensor zuerst anschließen und dann die gewünschte Anzahl ergänzen.
3. Zehn Minuten Daten, steigende ACKs, Queue-Abbau und eine 380-Hz-WAV prüfen.
4. Reale Datenträgerlatenz prüfen: unter Last darf die Sensor-Spool nicht stetig wachsen.
5. Cloud kurz unerreichbar machen: lokaler Empfang läuft weiter; nach Wiederkehr
   werden Daten nachgesendet. WLAN des Sensors und Cloud-Verbindung getrennt testen.
6. Neustart und Wiederaufnahme prüfen. Keine Vollständigkeit aus bloßem Online-Status ableiten.

WAV-Datei und SQLite sind getrennte Speicher. Ein Prozess-/Stromausfall genau
zwischen WAV-Schreibvorgang und SQLite-Commit kann bei Wiederholung zusätzliche
Samples erzeugen; keine transaktionsübergreifende Exactly-once-Garantie.
Ein echter Stromausfalltest auf dem Pi bleibt deshalb Teil der Feldabnahme.
