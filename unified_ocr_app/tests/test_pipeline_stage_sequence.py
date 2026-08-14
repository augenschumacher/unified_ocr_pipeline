"""Charakterisierungstest: die beobachtbare Abfolge eines Laufs.

Die Buchfuehrung von process_file (Workflow-Status, Manifest-Stufen,
Diagnostics) war kaum durch Tests abgedeckt. Dieser Test haelt die aktuelle,
nach aussen sichtbare Abfolge fest, damit Umbauten daran nichts still
veraendern. Er beschreibt Ist-Verhalten, keine Wunschvorstellung.
"""
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.config import AppConfig
from core.pipeline import PipelineOrchestrator


def _run(kind, tmp_path):
    events = []
    config = AppConfig(tmp_path)
    config.base_dir = tmp_path
    config.final_dir.mkdir(parents=True, exist_ok=True)

    llm = MagicMock()
    llm.vision_model = "vision"
    llm.glm_ocr_model = "Keins"
    llm.analysis_model = "analysis"

    orch = PipelineOrchestrator(
        config=config,
        llm_client=llm,
        organize_enabled=False,
        save_docx_enabled=False,
        save_json_enabled=False,
        stage_callback=lambda event: events.append((event["step"], event["state"])),
    )

    def fake_export(*args, **kwargs):
        pdf = config.final_dir / "out.pdf"
        pdf.write_bytes(b"%PDF-1.4")
        return {"pdf": pdf, "txt": None, "docx": None, "json": None}

    orch._extract_text_from_docx = MagicMock(return_value="Inhalt des Dokuments")
    orch._stage_analysis = MagicMock(
        return_value=({"title": "T", "document_type": "Rechnung"}, "name")
    )
    orch._stage_export = MagicMock(side_effect=fake_export)
    orch._stage_organize = MagicMock(return_value=([], ""))

    if kind == "pdf":
        orch._stage_prepare = MagicMock(return_value=tmp_path / "w.pdf")
        orch._stage_ocrmypdf = MagicMock(return_value=(tmp_path / "o.pdf", "ocr text"))
        orch._stage_docling = MagicMock(return_value=("docling", {1: "md"}))
        orch._stage_extract_pages = MagicMock(return_value=([], {1: "text"}))
        orch._stage_glm_ocr = MagicMock(return_value={})
        orch._stage_vision_review = MagicMock(return_value=({1: "vision"}, {}))
        orch._stage_fusion = MagicMock(return_value={1: "fused"})
        orch._stage_quality = MagicMock(return_value=("fused", {"quality_status": "ok"}))

    source = tmp_path / ("a.docx" if kind == "docx" else "a.pdf")
    source.write_text("x", encoding="utf-8")

    with patch("core.pipeline.shutil.move"):
        outcome = orch.process_file(source)
    return events, outcome


OFFICE_SEQUENCE = [
    ("job", "running"),
    ("input", "running"),
    ("input", "success"),
    ("ocr", "running"),
    ("ocr", "success"),
    ("quality", "skipped"),
    ("metadata", "running"),
    ("metadata", "warning"),
    ("export", "running"),
    ("export", "success"),
    ("archive", "running"),
    ("archive", "warning"),
    ("google_drive", "skipped"),
    ("synology", "skipped"),
    ("complete", "warning"),
]

PDF_SEQUENCE = [
    ("job", "running"),
    ("input", "running"),
    ("input", "success"),
    ("ocr", "running"),
    ("ocr", "success"),
    ("quality", "running"),
    ("quality", "success"),
    ("metadata", "running"),
    ("metadata", "warning"),
    ("export", "running"),
    ("export", "success"),
    ("archive", "running"),
    ("archive", "warning"),
    ("google_drive", "skipped"),
    ("synology", "skipped"),
    ("complete", "warning"),
]


def test_office_path_emits_the_expected_workflow_sequence(tmp_path):
    events, outcome = _run("docx", tmp_path)

    assert events == OFFICE_SEQUENCE
    assert outcome["status"] == "review_required"


def test_pdf_path_emits_the_expected_workflow_sequence(tmp_path):
    events, outcome = _run("pdf", tmp_path)

    assert events == PDF_SEQUENCE
    assert outcome["status"] == "review_required"


@pytest.mark.parametrize("kind", ["docx", "pdf"])
def test_every_started_step_reaches_a_terminal_state(kind, tmp_path):
    """Kein Schritt darf auf 'running' stehen bleiben."""
    events, _ = _run(kind, tmp_path)

    terminal = {"success", "warning", "error", "skipped"}
    running = [step for step, state in events if state == "running"]
    finished = {step for step, state in events if state in terminal}

    unfinished = [step for step in running if step != "job" and step not in finished]
    assert unfinished == [], f"Schritte ohne Abschluss: {unfinished}"
