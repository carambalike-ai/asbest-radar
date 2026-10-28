# Asbest-Radar Vorarlberg

Ort eingeben, die KI schaut sich jedes Dach im Umkreis auf dem Luftbild an und sortiert nach Verdacht auf Asbestzement.

## Starten (per Doppelklick)

**Mac:** Doppelklick auf `Start Mac.command`.
Beim allerersten Mal blockt macOS die Datei eventuell. Dann Rechtsklick auf die Datei, **Öffnen**, nochmal **Öffnen** bestätigen.

**Windows:** Doppelklick auf `Start Windows.bat`.

Beim ersten Start passiert automatisch (ca. 1 Minute):
- Fehlt Python, öffnet sich die Download-Seite. Installieren, dann nochmal doppelklicken. Unter Windows bei der Installation "Add python.exe to PATH" anhaken.
- Pillow wird selbst installiert.
- Du wirst einmal nach dem API-Schlüssel gefragt (console.anthropic.com, Bereich API Keys). Er wird in `api-key.txt` im Ordner gespeichert. Diese Datei nicht weitergeben.

Danach öffnet sich der Browser auf http://localhost:8765. Das schwarze Fenster offen lassen, solange du das Tool benutzt.

Die Analyse läuft über deinen eigenen API-Zugang und wird nach Verbrauch abgerechnet (pro Dach ein Bild mit rund 768 × 768 Pixeln).

## Bedienung

1. Ort oder Adresse eingeben, z.B. `Hard, Seestraße` oder `Lustenau Industriegebiet`, Suchen.
2. Umkreis und maximale Dächerzahl wählen.
3. **Dächer analysieren.**
4. Rechts erscheinen die Dächer sortiert: hoch, mittel, unklar, gering. Klick zoomt aufs Dach. Darunter kannst du selbst "bestätigt", "verworfen" oder "vor Ort prüfen" setzen.
5. Export als GeoJSON (für QGIS) oder CSV (für Excel). Jede Analyse wird zusätzlich im Ordner `ergebnisse` gespeichert.

## Wie lange dauert es

| Dächer | Dauer ungefähr |
|---|---|
| 20 | 1 bis 2 Minuten |
| 60 | 3 bis 5 Minuten |
| 250 | 12 bis 20 Minuten |

Die KI bewertet 6 Dächer pro Anfrage, 2 Anfragen gleichzeitig. Die Restzeit steht live in der Statuszeile.

## Datenquellen

- **Luftbild:** Echtfarben-Orthofoto 2022 des Landes Vorarlberg, 10 cm Bodenauflösung, Lizenz CC BY 4.0 (VoGIS). Ersatzweise basemap.at Orthofoto.
- **Gebäudeumrisse:** OpenStreetMap über Overpass. Fehlt ein Gebäude in OSM, wird es nicht bewertet.
- **Ortssuche:** OpenStreetMap Nominatim, auf Vorarlberg begrenzt.

## Grenzen

- Das Luftbild ist von 2022. Was seither saniert wurde, sieht die KI nicht.
- Aus einem Luftbild ist Asbest nie sicher feststellbar. Neue asbestfreie Faserzementplatten sehen ähnlich aus. Das Ergebnis ist eine Vorauswahl für die Begehung, kein Befund.
- Asbestzement-Dachprodukte dürfen in Österreich seit 1.1.1994 nicht mehr in Verkehr gebracht werden. Ältere graue Wellplatten und Faserzementschindeln sind deshalb der Hauptverdacht.

## Wenn etwas nicht geht

- **Luftbild bleibt grau:** Rechts oben auf der Karte auf basemap.at umschalten und bei Bildquelle ebenfalls basemap.at wählen.
- **Falscher API-Schlüssel:** `api-key.txt` im Ordner löschen und neu starten, dann wirst du wieder gefragt.
- **Anderes Modell:** `ASBEST_MODEL` setzen, z.B. `claude-opus-5-5` für genauere, aber langsamere und teurere Bewertung.
