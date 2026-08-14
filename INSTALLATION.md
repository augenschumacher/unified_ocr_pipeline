# Installation

## Fuer Anwender

1. `UnifiedOCR_Setup.exe` starten.
2. Falls Windows SmartScreen warnt, den Herausgeber und SHA256-Hash mit dem Release vergleichen.
3. Der Installer installiert Unified OCR unter `%LOCALAPPDATA%\Programs\UnifiedOCR`.
4. Tesseract OCR, Ghostscript und QPDF werden geprueft und bei Bedarf per WinGet installiert.
5. Ollama ist optional. Wenn lokale KI-Modelle gewuenscht sind, fragt der Installer nach der VRAM-Klasse und schlaegt passende Modelle vor.

## Clean-VM-Test vor Releases

Auf einer frischen Windows-VM:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File packaging\windows\test_clean_install.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File packaging\windows\test_clean_install.ps1 -RunInstaller
```

Danach pruefen:

- App startet ohne Python-Installation.
- Systemcheck findet Tesseract, Ghostscript, QPDF und OCRmyPDF.
- Ein kleines PDF und ein Bild koennen verarbeitet werden.
- Ollama-Auswahl kann abgebrochen werden.
- Deinstallation entfernt Programmdateien und Verknuepfungen.

## Signieren

Vor oeffentlichen Releases sollte die Setup-EXE signiert werden:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File packaging\windows\sign_release.ps1 -CertificateThumbprint <THUMBPRINT>
```

Danach den SHA256-Hash und die Signatur im GitHub Release veroeffentlichen.
