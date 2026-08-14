"""Tests fuer die aus process_file herausgeloesten Bausteine."""
import pytest

from core.pipeline import PipelineOrchestrator


def test_mark_review_deferred_sets_blocking_gate():
    report = PipelineOrchestrator._mark_review_deferred({})

    assert report["requires_review"] is True
    assert report["review_required"] is True
    assert report["quality_status"] == "review"
    assert report["severity"] == "warning"
    assert report["review"]["blocking"] is True
    assert report["review"]["auto_correction_allowed"] is False
    codes = {r["code"] for r in report["review_reasons"]}
    assert "manual_review_deferred" in codes


def test_mark_review_deferred_preserves_critical_state():
    report = PipelineOrchestrator._mark_review_deferred(
        {"quality_status": "critical", "severity": "error"}
    )

    assert report["quality_status"] == "critical"
    assert report["severity"] == "error"


def test_mark_review_deferred_is_idempotent():
    first = PipelineOrchestrator._mark_review_deferred({})
    second = PipelineOrchestrator._mark_review_deferred(first)

    codes = [r["code"] for r in second["review_reasons"]]
    assert codes.count("manual_review_deferred") == 1
    assert len(second["warnings"]) == 1


def test_mark_review_deferred_accepts_none():
    report = PipelineOrchestrator._mark_review_deferred(None)
    assert report["review_required"] is True


def test_mark_review_deferred_keeps_existing_reasons():
    report = PipelineOrchestrator._mark_review_deferred(
        {"review_reasons": [{"code": "ocr_language_preflight_incomplete"}]}
    )

    codes = {r["code"] for r in report["review_reasons"]}
    assert codes == {"ocr_language_preflight_incomplete", "manual_review_deferred"}


class _Config:
    large_pdf_page_limit = 20


class _Orchestrator(PipelineOrchestrator):
    def __init__(self):
        self.config = _Config()


def test_reduced_analysis_report_requires_review():
    report = _Orchestrator()._build_reduced_analysis_report(25)

    assert report["requires_review"] is True
    assert report["quality_score"] == 70
    assert report["review"]["reasons"] == report["review_reasons"]
    assert "25 > 20" in report["warnings"][0]


def test_extract_office_text_rejects_unknown_suffix(tmp_path):
    orchestrator = _Orchestrator()

    with pytest.raises(RuntimeError, match="kein Textextraktor"):
        orchestrator._extract_office_text(tmp_path / "x.rtf", ".rtf")
