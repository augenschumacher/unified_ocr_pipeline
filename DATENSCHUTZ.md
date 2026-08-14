# Datenschutz

Unified OCR verarbeitet private Dokumente. Behandle die Einstellungen daher bewusst.

## Lokal

Im Modus `local_only` werden fuer neue Jobs externe Cloud-LLMs, Google Drive und nicht-lokale WebDAV-Ziele deaktiviert. Lokale OCR, lokale Dateispeicherung und lokale Ollama-Modelle bleiben moeglich.

## Externe Dienste

Wenn Cloud-Modelle oder Sync aktiviert werden, koennen Dokumentinhalte oder erzeugte Dateien an externe Anbieter uebertragen werden:

- Google Drive: Upload der gewaehlten Ausgabedateien in dein Google-Konto.
- Google Gemini/OpenAI/Mistral: Dokumenttext oder Bild-/Textauszuege koennen an den jeweiligen API-Anbieter gesendet werden.
- Synology/WebDAV: Upload in das konfigurierte WebDAV-Ziel.

## Empfohlene Einstellung fuer sensible Dokumente

- Datenschutzmodus `local_only`
- lokale Ollama-Modelle statt Cloud-LLMs
- Google Drive nur aktivieren, wenn Upload ausdruecklich gewuenscht ist
- Diagnose- und Manifestdateien regelmaessig pruefen

API-Keys, Tokens und lokale Einstellungen liegen unter `%APPDATA%\UnifiedOCR` und duerfen nicht ins Repository oder in Support-Anfragen kopiert werden.

Synology/WebDAV-Passwoerter werden unter Windows nach Moeglichkeit im Windows
Credential Manager gespeichert. In `settings.json` bleibt dann nur eine lokale
Referenz. Falls diese Windows-Funktion nicht verfuegbar ist, kann ein lokaler
Fallback genutzt werden; in diesem Fall muss der App-Datenordner besonders
geschuetzt werden.

Die Wartungsfunktion `Laufzeitdaten bereinigen...` loescht nur ausgewaehlte
Laufzeitordner wie `work`, `error` und `logs`. Originale und finale Ergebnisse
werden dabei nicht entfernt.
