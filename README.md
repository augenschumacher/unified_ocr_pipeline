# Unified OCR App

Unified OCR App ist eine lokale Windows-Desktop-App fuer OCR, LLM-gestuetzte
Nachverarbeitung, Dokumentexport und optionale Google-Drive-Ablage.

Die App ist fuer private Dokumenten-Workflows gedacht: Eingangsordner
ueberwachen, Dokumente per OCR/Docling auslesen, Ergebnisse als PDF/TXT/DOCX
speichern, optional mit LLMs verbessern und in einer lokalen oder
Google-Drive-Ablagestruktur organisieren.

## Features

- Ueberwachter Eingangsordner fuer PDF, PNG, JPG, HEIC, DOCX, ODT, DOC und ODOC
- OCRmyPDF plus Docling-Extraktion
- Optionale LLM-Stufen fuer GLM-OCR, Vision-Review, Text-Fusion und Analyse
- Unterstuetzung fuer lokale Ollama-Modelle und API-Provider ueber LiteLLM
- Lokaler Export als PDF, TXT, DOCX und optional JSON
- Ablagestrukturverwaltung mit Personen/Hauptbereichen und Kategorien
- Optionaler Google-Drive-Upload und Google-Drive-Ordnersync
- Job-Historie unter `<Basisordner>/logs/job_history.jsonl`
- Runtime-Audit in Qualitaetsberichten mit Modellnamen, Optionen und Prompt-Fingerprints

## Status

Dieses Projekt ist ein Release Candidate. Es verarbeitet potenziell sensible
Dokumente lokal, kann aber bei aktivierten Cloud- oder API-Funktionen Daten an
die jeweils konfigurierten Dienste uebertragen.

Vor einer produktiven Nutzung sollten die Einstellungen, Google-Drive-Freigaben,
LLM-Provider und Ausgabedateien mit Testdokumenten geprueft werden.

## Voraussetzungen

- Windows
- Python 3.10
- Ghostscript
- Tesseract
- OCRmyPDF-kompatible Systemumgebung
- Optional: Ollama mit lokalen Modellen
- Optional: Google OAuth Desktop Client fuer Google Drive
- Optional: API-Keys fuer Gemini, OpenAI oder Mistral

## Installation

```powershell
cd <lokaler-projektordner>
py -3.10 -m venv .venv
.\.venv\Scripts\Activate.ps1
py -3.10 -m pip install --upgrade pip
py -3.10 -m pip install -r unified_ocr_app\requirements.txt
```

Alternativ kann die App fuer Entwicklung als lokales Paket installiert werden:

```powershell
py -3.10 -m pip install -e .\unified_ocr_app[dev]
```

## Start

```powershell
py -3.10 main.py
```

Alternativ:

```powershell
ocr_pipeline.bat
```

Nach einer lokalen Paketinstallation steht zusaetzlich der Konsolenbefehl zur
Verfuegung:

```powershell
unified-ocr
```

## Ersteinrichtung

Beim ersten Start oeffnet die App einen Setup-Wizard, solange die
Dokumentensortierung aktiv ist und noch keine Ablagepfade existieren.

- Primaerpfade sind Personen oder Hauptbereiche, zum Beispiel `Jan Beispiel`, `Laura Platzhalter` oder `Familie`.
- Die Standardvorlage legt Kategorien wie `Gesundheit`, `Finanzen`, `Versicherungen`, `Steuern`, `Schule`, `Wohnen` und `Sonstiges` unter jeder Person an.
- Beim Speichern werden die Registry `folder_registry.json` und die realen Zielordner unter `<Basisordner>/final` erstellt.
- Wenn die Dokumentensortierung deaktiviert ist, kann die Ueberwachung auch ohne Ablagepfade gestartet werden.
- Die Ablagestruktur kann spaeter in der App erweitert oder geaendert werden.

## LLM Provider

Die App unterstuetzt lokale Ollama-Modelle und API-Provider ueber LiteLLM.

Wichtig: Google Drive OAuth und Google Gemini API sind getrennt.

- Google Drive OAuth ist nur fuer Drive-Upload und Ordnersync zustaendig.
- Google Gemini benoetigt separat einen API-Key aus Google AI Studio.
- API-Keys werden lokal unter `%APPDATA%\UnifiedOCR\llm_config.yaml` gespeichert.
- Diese Datei darf nicht ins Repository hochgeladen werden.

Gemini einrichten:

1. Gemini API-Key in Google AI Studio erstellen.
2. In der App `API-Schluessel & Provider` oeffnen.
3. Key bei `Google Gemini API Key` eintragen.
4. Optional `Gemini testen` klicken.
5. Speichern und ein Gemini-Modell wie `gemini/gemini-2.5-flash` auswaehlen.

Wenn Gemini trotz Free Tier nicht funktioniert, sind typische Ursachen:

- Es wurde ein Google-Drive-OAuth-Client statt eines Gemini API-Keys eingetragen.
- Die Gemini API ist fuer das Projekt bzw. den Key nicht nutzbar.
- Das Kontingent ist erschoepft.
- Ein altes oder nicht verfuegbares Modell wurde ausgewaehlt.
- Der Key wurde nicht gespeichert oder die Modellliste noch nicht neu geladen.

## Google Drive

Google Drive ist optional. Die App funktioniert auch komplett ohne Drive-Upload.

Fuer Drive nutzt die App OAuth fuer Desktop-Anwendungen und den Scope:

```text
https://www.googleapis.com/auth/drive
```

Dieser Scope wird verwendet, weil die App Ordner aufloesen, fehlende Ordner
erstellen, Dateien hochladen und vorhandene Dateien aktualisieren kann.

### Google-Drive-Credentials erstellen

Erstelle die Credentials in deinem eigenen Google-Cloud-Projekt. Lade keine
Credentials in dieses Repository hoch und veroeffentliche keine privaten
Client-Secrets.

1. In der Google Cloud Console ein Projekt erstellen oder auswaehlen.
2. Google Drive API fuer dieses Projekt aktivieren.
3. OAuth-Zustimmungsbildschirm einrichten.
4. OAuth Client ID erstellen.
5. Als Anwendungstyp `Desktop app` waehlen.
6. JSON-Datei herunterladen.
7. Die Datei lokal als `credentials.json` speichern, empfohlen unter:

```text
%APPDATA%\UnifiedOCR\credentials.json
```

8. In der App diesen Pfad bei `credentials.json Pfad` auswaehlen.
9. `Google Drive verknuepfen` klicken und den Browser-Login abschliessen.

Nach erfolgreichem Login speichert die App den OAuth-Token unter:

```text
%APPDATA%\UnifiedOCR\google_drive_token.json
```

Auch diese Token-Datei darf nicht ins Repository hochgeladen werden.

Offizielle Google-Dokumentation:

- [Google Drive API Python Quickstart](https://developers.google.com/workspace/drive/api/quickstart/python)
- [Google Workspace: Create access credentials](https://developers.google.com/workspace/guides/create-credentials)

### Wichtiger Hinweis fuer Open-Source-Releases

Dieses Repository enthaelt bewusst keine `credentials.json`. Nutzerinnen und
Nutzer sollten ihre eigenen Google-OAuth-Credentials erstellen. Ein gemeinsam
veroeffentlichter OAuth-Client waere fuer eine Desktop-App schwer geheim zu
halten und koennte missbraucht werden.

Solange die Google-App nicht verifiziert ist, kann Google beim Login eine
Warnung anzeigen oder nur Testnutzer zulassen. Das ist normal fuer unverifizierte
OAuth-Apps.

## Laufzeitdaten

Benutzerspezifische Daten werden unter folgendem Ordner gespeichert:

```text
%APPDATA%\UnifiedOCR
```

Dort koennen insbesondere liegen:

- `settings.json`
- `llm_config.yaml`
- `credentials.json`
- `google_drive_token.json`

Der App-Ordner selbst sollte keine Tokens, Credentials, Logs oder lokalen
Settings enthalten. Die `.gitignore` schliesst diese Dateien aus, eine manuelle
Pruefung vor jedem Release bleibt trotzdem sinnvoll.

## Lizenz

Copyright (C) 2026 Fabio Schumacher.

Dieses Projekt ist freie Software unter der GNU Affero General Public License
v3.0 only, siehe [unified_ocr_app/LICENSE](unified_ocr_app/LICENSE).

Die Wahl von AGPL-3.0 passt zur PyMuPDF-Abhaengigkeit, die unter AGPL-3.0 oder
kommerzieller Artifex-Lizenz verfuegbar ist. Drittanbieter-Abhaengigkeiten sind
in [unified_ocr_app/THIRD_PARTY_LICENSES.md](unified_ocr_app/THIRD_PARTY_LICENSES.md) zusammengefasst.

Kurz praktisch: Wer diese App weitergibt oder veraenderte Versionen oeffentlich
betreibt, muss die AGPL-3.0-Pflichten beachten, insbesondere den
korrespondierenden Quellcode bereitstellen. Es gibt keine Garantie oder
Gewaehrleistung.

## Sicherheit

Bitte lies vor der Veroeffentlichung und produktiven Nutzung auch
[unified_ocr_app/SECURITY.md](unified_ocr_app/SECURITY.md).

Besonders wichtig:

- Keine echten Dokumente, Tokens, Credentials oder API-Keys committen.
- Besonders sensible Dokumente nur mit bewusst gewaehlten lokalen oder Cloud-Einstellungen verarbeiten.
- Google Drive nur mit einem Konto aktivieren, dessen Zugriff zu diesem Automationszweck passt.
- Qualitaetsberichte koennen technische Metadaten wie Modellnamen und Prompt-Fingerprints enthalten.

## Entwicklung und Tests

```powershell
py -3.10 -m pytest unified_ocr_app
```

Vor einem Release:

1. Tests ausfuehren.
2. App mit `ocr_pipeline.bat` starten.
3. Basisordner und Setup-Wizard pruefen.
4. Ein Testdokument in den Eingangsordner legen.
5. Ergebnisdateien, Qualitaetsbericht und Job-Historie pruefen.
6. Google Drive nur mit Testdaten pruefen.
7. Sicherstellen, dass keine Dateien wie `settings.json`, `llm_config.yaml`, `credentials.json`, `google_drive_token.json`, `token.json`, `.env`, `*.key` oder `*.pem` im Repository liegen.

## Bekannte Grenzen

- Die Qualitaet der OCR haengt stark von Scanqualitaet, Sprache und installierten OCR-Komponenten ab.
- LLM-Ergebnisse muessen bei wichtigen medizinischen, finanziellen oder rechtlichen Dokumenten manuell geprueft werden.
- Cloud- und API-Provider koennen Kosten verursachen oder Kontingente begrenzen.
- Google Drive Sync erstellt fehlende Ordner und speichert Drive-IDs, loescht aber keine Drive-Ordner.
- Wenn die dokumentweite Qualitaetskorrektur Text veraendert, bleibt der PDF-Textlayer seitenweise aus der urspruenglichen Fusion erhalten; der korrigierte Gesamttext wird fuer TXT/DOCX/Metadaten genutzt.
