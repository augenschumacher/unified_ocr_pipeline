from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import shutil

import pytest

from core.ocr.pdf_prep import (
    inspect_pdf_page_content,
    list_installed_tesseract_languages,
    normalize_ocr_languages,
    ocrmypdf_mode_api_options,
    ocrmypdf_mode_cli_args,
    resolve_ocr_languages,
    run_ocrmypdf,
)


@patch("core.ocr.pdf_prep.subprocess.run")
@patch("core.ocr.pdf_prep.get_ocrmypdf_command", return_value=["ocrmypdf"])
def test_default_auto_mode_preserves_born_digital_text_pages(mock_command, mock_run):
    mock_run.return_value = SimpleNamespace(returncode=0, stdout="", stderr="")

    run_ocrmypdf(
        Path("input.pdf"),
        Path("output.pdf"),
        Path("sidecar.txt"),
        languages="deu+eng",
    )

    command = mock_run.call_args.args[0]
    assert "--skip-text" in command
    assert "--force-ocr" not in command
    assert "--redo-ocr" not in command
    assert command[command.index("--output-type") + 1] == "pdfa-2"
    assert command[command.index("-l") + 1] == "deu+eng"


@pytest.mark.parametrize(
    ("mode", "cli_flag", "api_key"),
    [
        ("auto", "--skip-text", "skip_text"),
        ("redo", "--redo-ocr", "redo_ocr"),
        ("force", "--force-ocr", "force_ocr"),
    ],
)
def test_modes_are_mutually_exclusive(mode, cli_flag, api_key):
    assert ocrmypdf_mode_cli_args(mode) == [cli_flag]
    assert ocrmypdf_mode_api_options(mode) == {api_key: True}


@patch("core.ocr.pdf_prep._run_ocrmypdf_api")
@patch("core.ocr.pdf_prep.get_ocrmypdf_command", return_value=[])
def test_api_fallback_receives_same_redo_and_language_contract(
    mock_command,
    mock_api,
    tmp_path,
):
    sidecar = tmp_path / "sidecar.txt"
    sidecar.write_text("API text", encoding="utf-8")

    result = run_ocrmypdf(
        Path("input.pdf"),
        Path("output.pdf"),
        sidecar,
        mode="redo",
        languages=("deu", "eng", "deu"),
        rotate_pages=False,
    )

    assert result == "API text"
    kwargs = mock_api.call_args.kwargs
    assert kwargs["redo_ocr"] is True
    assert "skip_text" not in kwargs
    assert "force_ocr" not in kwargs
    assert kwargs["language"] == ["deu", "eng"]
    assert kwargs["output_type"] == "pdfa-2"
    assert kwargs["rotate_pages"] is False
    assert "rotate_pages_threshold" not in kwargs


def test_invalid_mode_and_language_are_rejected_before_ocr():
    with pytest.raises(ValueError, match="OCR-Modus"):
        ocrmypdf_mode_cli_args("guess")
    with pytest.raises(ValueError, match="Sprachcode"):
        normalize_ocr_languages("deu+../../eng")


@patch("core.ocr.pdf_prep.subprocess.run")
@patch("core.ocr.pdf_prep.shutil.which", return_value="tesseract")
def test_installed_tesseract_languages_are_parsed(mock_which, mock_run):
    mock_run.return_value = SimpleNamespace(
        returncode=0,
        stdout="List of available languages in tessdata (3):\ndeu\neng\nosd\n",
        stderr="",
    )

    assert list_installed_tesseract_languages() == ("deu", "eng", "osd")
    assert mock_run.call_args.args[0] == ["tesseract", "--list-langs"]


def test_missing_optional_language_is_reported_and_not_sent_to_ocr():
    result = resolve_ocr_languages("deu+fra", ("deu", "eng", "osd"))

    assert result["effective"] == ["deu"]
    assert result["missing"] == ["fra"]
    assert result["fallback_used"] is False
    assert "fra" in result["warnings"][0]


def test_deterministic_installed_fallback_and_strict_mode():
    fallback = resolve_ocr_languages("fra", ("eng", "deu", "osd"))
    assert fallback["effective"] == ["deu"]
    assert fallback["fallback_used"] is True

    with pytest.raises(RuntimeError, match="fra"):
        resolve_ocr_languages("fra", ("deu", "eng"), strict=True)


def test_pipeline_uses_preserved_embedded_text_when_sidecar_marks_page_skipped(tmp_path):
    fitz = pytest.importorskip("fitz")
    from core.config import AppConfig
    from core.pipeline import PipelineOrchestrator

    work_pdf = tmp_path / "born-digital.pdf"
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 100), "Vorhandener Digitaltext RE-4711")
    document.save(work_pdf)
    document.close()

    def fake_ocr(source, output, sidecar, **_kwargs):
        shutil.copy2(source, output)
        sidecar.write_text("[OCR skipped on page(s) 1]", encoding="utf-8")
        return "[OCR skipped on page(s) 1]"

    orchestrator = PipelineOrchestrator(AppConfig(str(tmp_path / "archive")), object())
    with patch("core.pipeline.run_ocrmypdf", side_effect=fake_ocr), patch(
        "core.pipeline.resolve_ocr_languages",
        return_value={
            "requested": ["deu"],
            "available": ["deu"],
            "effective": ["deu"],
            "missing": [],
            "fallback_used": False,
            "detection_available": True,
            "warnings": [],
        },
    ):
        output_pdf, text = orchestrator._stage_ocrmypdf(work_pdf, tmp_path)

    assert output_pdf.is_file()
    assert "Vorhandener Digitaltext RE-4711" in text
    assert "OCR skipped" not in text
    assert orchestrator._last_ocr_preflight["text_source"] == "embedded_pdf"


def test_skipped_page_without_extractable_text_never_uses_marker_as_document_text(tmp_path):
    fitz = pytest.importorskip("fitz")
    from core.config import AppConfig
    from core.pipeline import PipelineOrchestrator

    work_pdf = tmp_path / "broken-text-layer.pdf"
    document = fitz.open()
    document.new_page()
    document.save(work_pdf)
    document.close()

    def fake_ocr(source, output, sidecar, **_kwargs):
        shutil.copy2(source, output)
        marker = "[OCR skipped on page(s) 1]"
        sidecar.write_text(marker, encoding="utf-8")
        return marker

    orchestrator = PipelineOrchestrator(AppConfig(str(tmp_path / "archive")), object())
    with patch("core.pipeline.run_ocrmypdf", side_effect=fake_ocr), patch(
        "core.pipeline.resolve_ocr_languages",
        return_value={
            "requested": ["deu"],
            "available": ["deu"],
            "effective": ["deu"],
            "missing": [],
            "fallback_used": False,
            "detection_available": True,
            "warnings": [],
        },
    ):
        _output_pdf, text = orchestrator._stage_ocrmypdf(work_pdf, tmp_path)

    assert text == ""
    assert orchestrator._last_ocr_preflight["review_required"] is True
    assert orchestrator._last_ocr_preflight["unextractable_skipped_pages"] == [1]
    assert any(
        reason["code"] == "skipped_pdf_text_not_extractable"
        for reason in orchestrator._last_ocr_preflight["review_reasons"]
    )


def test_hybrid_pdf_page_is_detected_before_auto_mode(tmp_path):
    fitz = pytest.importorskip("fitz")
    from PIL import Image

    image_path = tmp_path / "scan.png"
    Image.new("RGB", (1200, 1600), "white").save(image_path)
    pdf_path = tmp_path / "hybrid.pdf"
    document = fitz.open()
    page = document.new_page(width=600, height=800)
    page.insert_image(page.rect, filename=str(image_path))
    page.insert_text((50, 40), "Digitaler Briefkopf")
    document.save(pdf_path)
    document.close()

    inspection = inspect_pdf_page_content(pdf_path)

    assert inspection["available"] is True
    assert inspection["hybrid_pages"] == [1]
    assert inspection["pages"][0]["image_area_ratio"] >= 0.99


def test_hybrid_scan_with_only_single_digital_page_number_is_still_detected(tmp_path):
    fitz = pytest.importorskip("fitz")
    from PIL import Image

    image_path = tmp_path / "scan-with-number.png"
    Image.new("RGB", (1200, 1600), "white").save(image_path)
    pdf_path = tmp_path / "hybrid-single-character.pdf"
    document = fitz.open()
    page = document.new_page(width=600, height=800)
    page.insert_image(page.rect, filename=str(image_path))
    page.insert_text((295, 780), "1")
    document.save(pdf_path)
    document.close()

    inspection = inspect_pdf_page_content(pdf_path)

    assert inspection["hybrid_pages"] == [1]
    assert inspection["pages"][0]["text_characters"] == 1


def test_language_or_hybrid_preflight_becomes_publication_review_gate(tmp_path):
    from core.config import AppConfig
    from core.pipeline import PipelineOrchestrator

    orchestrator = PipelineOrchestrator(AppConfig(str(tmp_path)), object())
    orchestrator._last_ocr_preflight = {
        "review_required": True,
        "review_reasons": [
            {
                "code": "ocr_language_preflight_incomplete",
                "severity": "warning",
                "message": "OCR-Sprache fehlt.",
            },
            {
                "code": "hybrid_pdf_pages_skipped_by_auto_mode",
                "severity": "warning",
                "message": "Hybride Seite prüfen.",
                "pages": [2],
            },
        ],
    }
    report = {
        "severity": "info",
        "quality_status": "ok",
        "quality_score": 100,
        "warnings": [],
        "review_reasons": [],
        "review": {"required": False, "blocking": False},
    }

    orchestrator._merge_ocr_preflight_quality(report)

    assert report["quality_status"] == "review"
    assert report["quality_score"] == 70
    assert report["requires_review"] is True
    assert report["review"]["required"] is True
    assert {reason["code"] for reason in report["review_reasons"]} == {
        "ocr_language_preflight_incomplete",
        "hybrid_pdf_pages_skipped_by_auto_mode",
    }
