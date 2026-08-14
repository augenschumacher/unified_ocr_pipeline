"""Zustand eines einzelnen Verarbeitungslaufs.

Ein ``PipelineOrchestrator`` wird fuer viele Dokumente wiederverwendet: der
Watchdog-Worker holt Datei um Datei aus einer Queue, der manuelle Stapel laeuft
in einer Schleife.  Der Zustand eines Dokuments lag bisher als lose Attribute
auf dem Orchestrator und musste am Anfang von ``process_file`` von Hand
zurueckgesetzt werden.  Vier dieser Attribute wurden dabei uebersehen, wodurch
zum Beispiel ein Office-Dokument die OCR-Preflight-Warnung des zuvor
verarbeiteten PDFs erbte.

Mit diesem Container wird pro Dokument ein frisches Objekt erzeugt.  Neue
Zustandsfelder sind damit automatisch sauber, ohne dass jemand an einen
Reset-Block denken muss.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class JobContext:
    """Alle Werte, die nur fuer ein einziges Dokument gelten."""

    # Identitaet des laufenden Auftrags
    job_id: str = ""
    source_name: str = ""
    original_path: Path | None = None
    manifest_required: bool = False

    # Entscheidungen aus Review und Einsortierung
    organization_deferred: bool = False
    manual_review_completed: bool = False
    chosen_target_path: str | None = None

    # Zwischenergebnisse einzelner Stufen
    active_workflow_step: str = ""
    analysis_source_pages: dict | None = None
    ocr_preflight: dict[str, Any] = field(default_factory=dict)
    export_final_name: str = ""
    rejected_filename_titles: list[dict] = field(default_factory=list)

    # Ergebnisse von Ablage und Synchronisierung
    organize_audit: list[dict] = field(default_factory=list)
    google_drive_summary: dict | None = None
    synology_summary: dict | None = None


@dataclass
class ExtractionResult:
    """Ergebnis der Textgewinnung (Stufen 1 bis 5).

    Nur diese fuenf Werte werden von den nachfolgenden Phasen gebraucht. Alles
    andere - Seitenbilder, Docling-Markdown, Vision-Ergebnisse, Layoutbloecke -
    bleibt in der Phase und landet ueber Manifest und Diagnostics im Auditpfad.
    """

    ocr_text: str = ""
    fused_text: str = ""
    fused_pages: dict = field(default_factory=dict)
    image_paths: list = field(default_factory=list)
    quality_report: dict = field(default_factory=dict)
    source_pdf_for_export: Path | None = None
