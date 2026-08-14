# Release-Haertung

Diese Datei bildet die Prueferpunkte als Release-Checkliste ab.

1. Clean-System-Installer-Test: `packaging/windows/test_clean_install.ps1` in einer frischen Windows-VM ausfuehren.
2. Dependency-Installation: Tesseract, Ghostscript und QPDF werden im Installer geprueft; WinGet-Ausfall muss mit manuellen Befehlen gemeldet werden.
3. Code Signing: `packaging/windows/sign_release.ps1` ausfuehren und Signatur/SHA256 im Release dokumentieren.
4. End-to-End-Tests: kleine PDF-/Bild-/Layout-Dokumente als Smoke-Fixtures nutzen und Pipeline-Artefakte pruefen.
5. Ollama-Modellkatalog: Empfehlungen liegen in `unified_ocr_app/resources/ollama_model_recommendations.json`.
6. Speicherplatz: Installer prueft freien Speicher vor `ollama pull`.
7. OCR-Qualitaet: Quality-Report, Diagnosebericht und Review-Queue vor Freigabe pruefen.
8. Mehrspaltige PDFs: Layout- und Lesereihenfolge mit echten Fachbuchseiten testen.
9. Updates: Versionsnummer, GitHub Release, Installer-Upgrade und Deinstallation pruefen.
10. Google Drive Sync: `created`/`updated`, Konflikte, Retry und Upload-Audit im Manifest kontrollieren.
11. Datenschutz: `DATENSCHUTZ.md` und UI-Hinweise aktuell halten.
12. Fehlerdialoge: technische Fehler in Handlungsempfehlungen uebersetzen.
13. Systemcheck: Beim Erststart Pflichtkomponenten sichtbar pruefen.
14. Sortiererkennung: Score, Begruendung und Lernspeicher pruefbar halten.
15. Backup/Recovery: Originale unveraendert sichern; lokale Zielkonflikte ohne Ueberschreiben testen.
16. Ressourcensteuerung: konfigurierbare PDF-Seitengrenze, grosse PDFs, Modellentladung und Queue-Verhalten testen.
17. UI: Erster Bildschirm muss Status, Eingang, Review und Sync verstaendlich zeigen.
18. Kurzdokumentation: `INSTALLATION.md`, `ERSTE_SCHRITTE.md`, `DATENSCHUTZ.md`.
19. Lizenzen: AGPL, Drittanbieter und Modelllizenzen vor Release pruefen.
20. Release-Reihenfolge: keine neuen Features freigeben, bevor Installer, Tests und Datenschutz gruen sind.
