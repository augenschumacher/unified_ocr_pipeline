# Unified OCR App

Unified OCR App ist eine lokale Windows-Desktop-App fuer OCR, LLM-gestuetzte
Nachverarbeitung, Dokumentexport und optionale Google-Drive- oder
Synology/WebDAV-Ablage.

Die App ist fuer private Dokumenten-Workflows gedacht: Eingangsordner
ueberwachen, Dokumente per OCR/Docling auslesen, Ergebnisse als PDF/TXT/DOCX
speichern, optional mit LLMs verbessern und in einer lokalen, Google-Drive-
oder Synology/WebDAV-Ablagestruktur organisieren.

## Features

- Ueberwachter Eingangsordner fuer PDF, PNG, JPG, HEIC, DOCX, ODT, DOC und ODOC
- OCRmyPDF plus Docling-Extraktion
- Optionale LLM-Stufen fuer GLM-OCR, Vision-Review, Text-Fusion und Analyse
- Unterstuetzung fuer lokale Ollama-Modelle und API-Provider ueber LiteLLM
- Lokaler Export als PDF, TXT, DOCX und optional JSON
- Ablagestrukturverwaltung mit Personen/Hauptbereichen und Kategorien
- Kontextbasierte Sortierhinweise pro Pfad, z. B. Fahrzeuge, Hobbys, Kennzeichen oder Aliase
- Progressiver Lernspeicher fuer bestaetigte Sortierentscheidungen mit mehreren Pfadvorschlaegen
- Lokale SQLite-Datenbank fuer Job-State, Dokumentindex, Review-Queue und Duplikaterkennung
- Systemcheck fuer Python, Tesseract, OCRmyPDF, Ghostscript und Schreibrechte
- Optionale Redaction sensibler Texte vor externen LLM-Aufrufen
- Optionaler Google-Drive-Upload und Google-Drive-Ordnersync
- Optionaler Synology/WebDAV-Upload mit gleicher Ablagestruktur
- Verbesserter PDF-Textlayer mit blockweiser Lesereihenfolge fuer mehrspaltige Seiten
- Job-Historie unter `<Basisordner>/logs/job_history.jsonl`
- Job-Manifeste pro Verarbeitungslauf mit Stage-Status, Artefakten, Hashes und Sync-Upload-Audit
- Optionale Debug-/Diagnoseberichte pro Job mit Stage-Dauern, Textquellen-Statistiken, Layoutpaketen und Output-Hashes
- Datenschutzmodus `local_only`, der Cloud-LLMs, Google Drive und nicht-lokale WebDAV-Ziele fuer neue Jobs deaktiviert
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
- Optional: Synology WebDAV Server, empfohlen per HTTPS auf Port 5006
- Optional: API-Keys fuer Gemini, OpenAI oder Mistral

## Installation

```powershell
cd <lokaler-projektordner>
py -3.10 -m venv .venv
.\.venv\Scripts\Activate.ps1
py -3.10 -m pip install --upgrade pip
py -3.10 -m pip install -r requirements.txt
```

Alternativ kann die App fuer Entwicklung als lokales Paket installiert werden:

```powershell
py -3.10 -m pip install -e .[dev]
```

## Start

Aus dem **Paketordner** `unified_ocr_app\` heraus:

```powershell
py -3.10 app.py
```

Aus dem **Repository-Root** (empfohlen fuer den normalen Betrieb):

```powershell
py -3.10 main.py
```

Alternativ per Batch-Datei:

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

### Intelligente Sortier-Kontexte

In der Ablagestrukturverwaltung kann zu jedem Pfad ueber `+Info` ein
Sortier-Kontext hinterlegt werden. Diese Informationen werden lokal in
`folder_registry.json` gespeichert und bei der automatischen Einordnung
beruecksichtigt.

Typische Beispiele:

- `Fabio/Auto/Golf`: Objekttyp `vehicle`, Aliase `Golf 7`, Stichworte `AB CD 123`, `Inspektion`, `HU`
- `Fabio/Auto/Tesla`: Objekttyp `vehicle`, Aliase `Model 3`, Stichworte `EF GH 456`, `Supercharger`
- `Jan/Hobby/Tennis`: Objekttyp `hobby`, Aliase `Tennisverein`, Stichworte `Mitgliedsbeitrag`, `Training`

Wenn ein Dokument eindeutig zu einem Kontext passt, wird dieser Pfad vor der
LLM-Klassifikation bevorzugt. Bei unsicheren Treffern greift weiterhin die
bestehende LLM-Klassifikation und der Review-/Staging-Mechanismus.

Zusaetzlich fuehrt die App einen lokalen Lernspeicher
`classification_memory.json`. Wenn ein unsicherer Vorschlag bestaetigt oder
korrigiert wird, merkt sich die App die Entscheidung mit relevanten Begriffen
aus Text und Metadaten. Bei spaeteren aehnlichen Dokumenten entstehen daraus
mehrere Pfadvorschlaege mit Score und Begruendung. Nur unsichere oder sehr nahe
Kandidaten werden abgefragt; eindeutige Treffer werden automatisch einsortiert.

Unsichere Sortierungen und neue Pfadvorschlaege werden zusaetzlich in der
lokalen Review-Queue gespeichert. Die App zeigt mehrere Kandidaten mit Score
und Begruendung an. Bestaetigte oder korrigierte Entscheidungen verbessern den
Lernspeicher.

## LLM Provider

Die App unterstuetzt lokale Ollama-Modelle und API-Provider ueber LiteLLM.

In den Einstellungen kann der Datenschutzmodus auf `local_only` gesetzt werden.
Dann werden fuer neue Jobs externe Cloud-Modelle, Google Drive und nicht-lokale
WebDAV-Ziele deaktiviert. Lokale NAS-Adressen wie `nas.local`, `diskstation`
oder private IP-Adressen koennen weiter genutzt werden. Das ist der empfohlene
Modus fuer besonders sensible Dokumente, wenn ausschliesslich lokale
Verarbeitung gewuenscht ist.

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

## Synology / WebDAV

Synology ist optional und nutzt den WebDAV Server deiner DiskStation. Die App
erstellt fehlende Zielordner per WebDAV `MKCOL` und laedt Dateien per `PUT`
hoch. Bereits vorhandene Dateien werden auf WebDAV-Seite ueberschrieben bzw.
aktualisiert.

Empfohlene Einrichtung:

1. Auf der Synology den WebDAV Server installieren und aktivieren.
2. HTTPS/WebDAV auf Port `5006` verwenden.
3. Einen eigenen Benutzer fuer die App anlegen.
4. Diesem Benutzer Schreibrechte nur auf den gewuenschten Zielordner geben.
5. In der App `Synology / WebDAV Upload` aktivieren.
6. WebDAV-URL eintragen, z. B. `https://nas.local:5006`.
7. Optional eine Zielwurzel eintragen, z. B. `OCR`.
8. Benutzername und Passwort eintragen und `Verbindung testen` klicken.

Hinweis: Das Passwort wird derzeit in den lokalen App-Einstellungen gespeichert.
Diese Datei gehoert niemals ins Repository. Fuer eine spaetere harte
Produktversion waere der Windows Credential Manager die bessere Ablage.

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
- `classification_memory.json`
- `unified_ocr.sqlite3`

Pro Job erzeugt die App zusaetzlich ein Manifest. Bei erfolgreichen Jobs wird
es unter `<Basisordner>/final/begleitdateien/*_job_manifest.json` abgelegt, bei
fehlgeschlagenen Jobs im Error-Bereich. Darin stehen Eingabe-Hashes,
Pipeline-Stages, erzeugte Ausgabepfade, Metadaten und optional Google-Drive-
Upload-IDs.

Wenn `Debug-/Diagnoseberichte speichern` aktiviert ist, schreibt die App
zusaetzlich `<Basisordner>/final/begleitdateien/*_debug_report.json`; bei
Fehlern landet der Bericht im Error-Bereich. Der Diagnosebericht enthaelt
Stage-Dauern, Modellnamen, Textlaengen, SHA-256-Hashes, kurze Textvorschauen,
Layoutpakete, Output-Dateigroessen und Sync-Ergebnisse. API-Keys, Tokens,
Passwoerter und Credentials werden redigiert.

Die Datei `unified_ocr.sqlite3` enthaelt lokale Laufzeitdaten wie Job-Zustaende,
Dokumentenindex, Duplikat-Referenzen und Review-Queue. Sie ist privat und sollte
nicht veroeffentlicht werden.

Der App-Ordner selbst sollte keine Tokens, Credentials, Logs oder lokalen
Settings enthalten. Die `.gitignore` schliesst diese Dateien aus, eine manuelle
Pruefung vor jedem Release bleibt trotzdem sinnvoll.

## Lizenz

Copyright (C) 2026 Fabio Schumacher.

Dieses Projekt ist freie Software unter der GNU Affero General Public License
v3.0 only, siehe [LICENSE](LICENSE).

Die Wahl von AGPL-3.0 passt zur PyMuPDF-Abhaengigkeit, die unter AGPL-3.0 oder
kommerzieller Artifex-Lizenz verfuegbar ist. Drittanbieter-Abhaengigkeiten sind
in [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md) zusammengefasst.

Kurz praktisch: Wer diese App weitergibt oder veraenderte Versionen oeffentlich
betreibt, muss die AGPL-3.0-Pflichten beachten, insbesondere den
korrespondierenden Quellcode bereitstellen. Es gibt keine Garantie oder
Gewaehrleistung.

## Sicherheit

Bitte lies vor der Veroeffentlichung und produktiven Nutzung auch
[SECURITY.md](SECURITY.md).

Besonders wichtig:

- Keine echten Dokumente, Tokens, Credentials oder API-Keys committen.
- Besonders sensible Dokumente nur mit bewusst gewaehlten lokalen oder Cloud-Einstellungen verarbeiten.
- Google Drive nur mit einem Konto aktivieren, dessen Zugriff zu diesem Automationszweck passt.
- Qualitaetsberichte koennen technische Metadaten wie Modellnamen und Prompt-Fingerprints enthalten.

## Entwicklung und Tests

```powershell
py -3.10 -m pytest
```

Vor einem Release:

1. Tests ausfuehren.
2. App mit `ocr_pipeline.bat` starten.
3. Basisordner und Setup-Wizard pruefen.
4. Ein Testdokument in den Eingangsordner legen.
5. Ergebnisdateien, Qualitaetsbericht und Job-Historie pruefen.
6. Job-Manifest im Ordner `begleitdateien` pruefen.
7. Google Drive nur mit Testdaten pruefen.
8. Sicherstellen, dass keine Dateien wie `settings.json`, `llm_config.yaml`, `credentials.json`, `google_drive_token.json`, `token.json`, `.env`, `*.key` oder `*.pem` im Repository liegen.

## Bekannte Grenzen

- Die Qualitaet der OCR haengt stark von Scanqualitaet, Sprache und installierten OCR-Komponenten ab.
- LLM-Ergebnisse muessen bei wichtigen medizinischen, finanziellen oder rechtlichen Dokumenten manuell geprueft werden.
- Cloud- und API-Provider koennen Kosten verursachen oder Kontingente begrenzen.
- Google Drive Sync erstellt fehlende Ordner und speichert Drive-IDs, loescht aber keine Drive-Ordner.
- Synology/WebDAV Sync erstellt fehlende Ordner und laedt Dateien in die gleiche Zielstruktur hoch.
- Der finale PDF-Textlayer wird blockweise aufgebaut. Bei typischen zweispaltigen Seiten wird links vor rechts gelesen; sehr komplexe Layouts sollten weiterhin stichprobenartig geprueft werden.
- Wenn die dokumentweite Qualitaetskorrektur Text veraendert, bleibt der PDF-Textlayer seitenweise aus der urspruenglichen Fusion erhalten; der korrigierte Gesamttext wird fuer TXT/DOCX/Metadaten genutzt.
