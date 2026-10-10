# Paket für das spätere Gateway-Wartungsfenster

Bestehende Gateways werden durch dieses Paket weder aktualisiert noch neu gestartet. Vor Installation lokale ausstehende WAVs und Messdaten sichern; nur das vereinbarte Wartungsfenster verwenden.

| Änderung | Für Peters Problem nötig? |
|---|---|
| WAV-Schlussprüfung mit tatsächlicher Samplezahl und Warnung | Ja, für die lokale frühzeitige Erkennung |
| Kleine Begleitdatei mit Aufnahmefenster und Schlussgrund | Hilft beim Unterscheiden regulärer Teilstücke |
| Übertragung der realen Aufnahmezeiten; alte WAVs weiterhin unterstützt | Ja, für neue zuverlässige Zeitfenster |
| Deprecated BLE-Worker wird auch bei alter Umgebungsflagge nicht gestartet | Ja, beendet die unnötigen Provisionierungsversuche |
| HTTPS ergibt WSS; alte HTTP:443-Konfiguration wird auf TLS korrigiert | Ja, korrigiert Port-443-Protokollfehler |
| Sampling, PCM-Umrechnung und bestätigte Löschung nach Upload | Unverändert |

Eine fehlgeschlagene Diagnose-Begleitdatei blockiert keine WAV-Fertigstellung oder Datenübertragung. Verkürzte WAVs bleiben uploadfähig. Unbestätigte Messdaten werden weiter aufbewahrt und erneut übertragen. Warnung `WAV_INCOMPLETE` enthält erhaltene/erwartete Samples, fehlende Sekunden und Schlussgrund.

Die Server-Vollständigkeitsanzeige bewertet alte WAVs anhand des nominalen Zehnminutenfensters. Alte Dateien enthalten keinen eindeutigen Schlussgrund; das Dashboard kennzeichnet deshalb Teilstück oder Datenlücke, nicht eine bewiesene Funkverlustursache. Bestehende Coverage-, Archiv- und Aufbewahrungsfelder werden nicht verändert.

Ohne wieder eingeschalteten Büro-Gateway und Funkstreckenmessung ist die Ursache der damaligen 15–25 % Verluste weiterhin offen. Der Wechsel zu Direct beseitigt diese Gateway-Strecke; die OLED-Anpassung behebt keine Datenverluste.
