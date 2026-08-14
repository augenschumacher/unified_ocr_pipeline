# Erste Schritte

1. Unified OCR starten.
2. Im Systemcheck pruefen, ob alle Pflichtkomponenten bereit sind.
3. Basisordner waehlen. Die App legt dort `consume`, `final`, `original`, `work`, `logs` und `error` an.
4. Ablagestruktur anlegen, z. B. Personen und Hauptbereiche.
5. Datenschutzmodus waehlen:
   - `local_only`: keine Cloud-LLMs, kein Google Drive, keine externen WebDAV-Ziele fuer neue Jobs.
   - `standard`: lokale Verarbeitung plus explizit aktivierte Cloud-/Sync-Funktionen.
6. Dokumente in den Eingangsordner legen oder weitere Eingangsordner konfigurieren.
7. Bei schwacher Hardware die Seitengrenze fuer reduzierte Analyse grosser PDFs konservativ setzen.
8. Unsichere Sortierungen in der Review-Queue pruefen und bestaetigen.

Tipp: Starte mit wenigen Testdokumenten, bevor du viele private Dokumente automatisch verarbeiten laesst.
