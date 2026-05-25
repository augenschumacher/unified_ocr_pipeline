# Sicherheit

## Lokale Verarbeitung

Die App ist für lokale OCR- und LLM-Verarbeitung ausgelegt. Dokumentinhalte werden an lokal konfigurierte Tools und Modelle übergeben. Google Drive wird nur genutzt, wenn die Option in den Einstellungen aktiv ist.

## Tokens und Credentials

OAuth-Tokens werden standardmäßig unter `%APPDATA%\UnifiedOCR\google_drive_token.json` gespeichert. Der App-Ordner darf keine folgenden Dateien enthalten:

- `token.json`
- `credentials.json`
- `client_secret*.json`
- lokale `settings.json`

Die `.gitignore` schließt diese Dateien aus. Vor einer Weitergabe oder Veröffentlichung sollte der App-Ordner trotzdem manuell geprüft werden.

API-Keys fuer LLM-Provider werden lokal unter `%APPDATA%\UnifiedOCR\llm_config.yaml` gespeichert. Diese Datei darf nicht ins Repository hochgeladen werden. Google Drive OAuth und Google Gemini API sind getrennte Zugangswege; fuer Gemini wird ein separater API-Key aus Google AI Studio benoetigt.

## Google Drive

Die App nutzt aktuell den Google-Drive-Scope `https://www.googleapis.com/auth/drive`, weil sie Ordner auflösen, erstellen und Dateien aktualisieren kann. Aktiviere Google Drive nur mit einem Konto, dessen Zugriff zu diesem Automationszweck passt.

Die Ablagestruktur kann über einen expliziten Sync-Schritt mit Google Drive abgeglichen werden. Dieser Schritt erstellt fehlende Ordner und speichert Drive-Ordner-IDs in der Registry. Er löscht keine Drive-Ordner. Wenn Google Drive mehrere gleichnamige Ordner an derselben Stelle enthält, meldet die App einen Konflikt und verwendet den ersten Treffer.

## Qualitätsberichte

Qualitätsberichte enthalten Runtime-Audit-Metadaten mit Modellnamen, Optionen und Prompt-Fingerprints. Die vollständigen Prompts werden nicht in den Auditdaten gespeichert.

## Umgang mit sensiblen Dokumenten

- Verwende einen lokalen Arbeitsordner auf einem verschlüsselten Laufwerk.
- Verarbeite besonders sensible Dokumente ohne Cloud-Upload.
- Lösche alte Dateien aus `original`, `final`, `error` und `logs`, wenn sie nicht mehr benötigt werden.
- Prüfe stichprobenartig OCR-Ergebnisse, besonders bei medizinischen oder finanziellen Dokumenten.
