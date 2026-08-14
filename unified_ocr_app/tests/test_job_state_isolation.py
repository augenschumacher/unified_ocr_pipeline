"""Beweist Zustandsuebertragung zwischen zwei Dokumenten desselben Laufs.

Ein PipelineOrchestrator wird fuer viele Dateien wiederverwendet (Watchdog-
Worker und manueller Batch). Pro-Job-Zustand, der nicht zurueckgesetzt wird,
wirkt deshalb auf das naechste Dokument weiter.
"""
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.config import AppConfig
from core.pipeline import PipelineOrchestrator


def _orchestrator(tmpdir_path, **kwargs):
    config = AppConfig(tmpdir_path)
    config.base_dir = tmpdir_path
    config.final_dir.mkdir(parents=True, exist_ok=True)

    llm = MagicMock()
    llm.vision_model = "vision-model"
    llm.glm_ocr_model = "Keins"
    llm.analysis_model = "analysis-model"

    orch = PipelineOrchestrator(
        config=config,
        llm_client=llm,
        save_docx_enabled=False,
        save_json_enabled=False,
        organize_enabled=False,
        **kwargs,
    )
    orch._extract_text_from_docx = MagicMock(return_value="Inhalt des Office-Dokuments")
    orch._stage_analysis = MagicMock(return_value=({"title": "Doc B"}, "doc_b"))
    orch._stage_export = MagicMock(return_value={})
    orch._stage_organize = MagicMock(return_value=([], ""))
    return orch


@patch("core.pipeline.shutil.move")
def test_office_document_does_not_inherit_previous_ocr_preflight(_mock_move):
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        orch = _orchestrator(tmpdir_path)

        # Zustand, den ein vorheriges PDF mit Sprachproblem hinterlassen haette.
        orch._last_ocr_preflight = {
            "review_required": True,
            "review_reasons": [{
                "code": "ocr_language_preflight_incomplete",
                "severity": "warning",
                "message": "Stammt aus dem VORHERIGEN Dokument.",
            }],
            "warnings": ["Stammt aus dem VORHERIGEN Dokument."],
        }

        office_file = tmpdir_path / "zweites_dokument.docx"
        office_file.write_text("docx", encoding="utf-8")
        orch.process_file(office_file)

        quality_report = orch._stage_export.call_args[0][6]
        reasons = {r.get("code") for r in (quality_report or {}).get("review_reasons", [])}

        assert "ocr_language_preflight_incomplete" not in reasons, (
            "Das Office-Dokument hat die OCR-Preflight-Warnung des vorherigen Dokuments geerbt."
        )
        assert not (quality_report or {}).get("ocr_preflight"), (
            "Fremde Preflight-Daten stehen im Qualitaetsbericht."
        )


@patch("core.pipeline.shutil.move")
def test_job_does_not_inherit_previous_sync_summary(_mock_move):
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        orch = _orchestrator(tmpdir_path)

        # Ein frueherer Upload war fehlgeschlagen.
        orch._last_google_drive_summary = {"state": "error", "message": "vorheriger Job"}
        orch._last_synology_summary = {"state": "error", "message": "vorheriger Job"}

        office_file = tmpdir_path / "drittes_dokument.docx"
        office_file.write_text("docx", encoding="utf-8")
        outcome = orch.process_file(office_file)

        assert outcome["status"] != "sync_failed", (
            "Der Job gilt als Sync-Fehler wegen eines fremden, alten Upload-Ergebnisses."
        )


def _bare_orchestrator(tmp_path):
    config = AppConfig(tmp_path)
    config.base_dir = tmp_path
    return PipelineOrchestrator(config=config, llm_client=MagicMock())


def test_begin_job_resets_every_context_field(tmp_path):
    """Jedes Feld des JobContext muss pro Dokument frisch sein."""
    import dataclasses

    from core.job_context import JobContext

    orch = _bare_orchestrator(tmp_path)

    # Alle Felder mit erkennbaren Fremdwerten belegen.
    sentinels = {
        "job_id": "alter-job",
        "source_name": "altes_dokument.pdf",
        "original_path": tmp_path / "alt.pdf",
        "manifest_required": True,
        "organization_deferred": True,
        "manual_review_completed": True,
        "chosen_target_path": "Alt/Pfad",
        "active_workflow_step": "export",
        "analysis_source_pages": {1: "alt"},
        "ocr_preflight": {"review_required": True},
        "export_final_name": "alter_name",
        "rejected_filename_titles": [{"title": "alt"}],
        "organize_audit": [{"action": "move_failed"}],
        "google_drive_summary": {"state": "error"},
        "synology_summary": {"state": "error"},
    }
    field_names = {f.name for f in dataclasses.fields(JobContext)}
    assert set(sentinels) == field_names, (
        "JobContext hat neue Felder; der Test muss sie mit abdecken."
    )
    for name, value in sentinels.items():
        setattr(orch._job, name, value)

    orch._begin_job(manifest_required=True)

    fresh = JobContext(manifest_required=True)
    for field in dataclasses.fields(JobContext):
        assert getattr(orch._job, field.name) == getattr(fresh, field.name), (
            f"Feld {field.name!r} wurde nicht zurueckgesetzt."
        )


def test_legacy_attribute_names_still_map_to_the_context(tmp_path):
    """Die alten Attributnamen bleiben die Schnittstelle fuer GUI und Tests."""
    orch = _bare_orchestrator(tmp_path)

    orch._current_job_id = "job-1"
    orch._last_ocr_preflight = {"review_required": True}
    orch._last_organize_audit = [{"action": "moved"}]
    orch._chosen_target_path = "Jan/Finanzen"

    assert orch._job.job_id == "job-1"
    assert orch._job.ocr_preflight == {"review_required": True}
    assert orch._job.organize_audit == [{"action": "moved"}]
    assert orch._job.chosen_target_path == "Jan/Finanzen"

    orch._begin_job()

    assert orch._current_job_id == ""
    assert orch._last_ocr_preflight == {}
    assert orch._last_organize_audit == []
    assert orch._chosen_target_path is None
