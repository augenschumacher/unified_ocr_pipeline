import json
from pathlib import Path

import pytest

from core.config import AppConfig
from core.exporter import DocumentExporter
from core.manifest import JobManifest


fitz = pytest.importorskip("fitz")


def _make_pdf(path: Path) -> None:
    doc = fitz.open()
    page = doc.new_page(width=300, height=420)
    page.insert_text((36, 72), "Unified OCR Smoke Test", fontsize=14)
    doc.save(path)
    doc.close()


def test_release_smoke_pdf_export_and_manifest(tmp_path):
    config = AppConfig(tmp_path)
    config.ensure_directories()
    source_pdf = tmp_path / "input.pdf"
    _make_pdf(source_pdf)

    exporter = DocumentExporter(
        config=config,
        output_format="PDF und TXT",
        docx_mode="Lesbare DOCX",
        save_docx_enabled=False,
        save_json_enabled=True,
        gdrive_enabled=False,
        gdrive_upload_docx=False,
        gdrive_upload_json=False,
        log_callback=lambda _message: None,
    )
    metadata = {
        "date": "20-06-2026",
        "title": "Smoke_Test",
        "document_type": "Test",
        "tags": "smoke,release",
    }
    quality_report = {"warnings": [], "quality_score": 100}

    outputs = exporter.export(
        source_pdf,
        {1: "Finaler Smoke-Test Text fuer den kopierbaren PDF-Layer."},
        "Finaler Smoke-Test Text fuer den kopierbaren PDF-Layer.",
        "2026-06-20_Smoke_Test",
        metadata,
        [],
        quality_report,
    )

    manifest = JobManifest.create(job_id="smoke-job", source_path=source_pdf, manifest_dir=tmp_path / "work")
    manifest.record_stage("export", "ok", artifacts=outputs)
    manifest.record_outputs(outputs)
    manifest.record_metadata(metadata)
    manifest.finalize("completed")
    manifest_path = manifest.write_copy(config.final_dir / "begleitdateien" / "smoke_manifest.json")

    assert outputs["pdf"].exists()
    assert outputs["txt"].exists()
    assert outputs["json"].exists()
    assert "Finaler Smoke-Test" in outputs["txt"].read_text(encoding="utf-8")

    exported_doc = fitz.open(outputs["pdf"])
    copied_text = "\n".join(page.get_text("text") for page in exported_doc)
    exported_doc.close()
    assert "Unified OCR Smoke Test" in copied_text
    assert "Finaler Smoke-Test Text" not in copied_text

    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert data["status"] == "completed"
    assert data["outputs"]["pdf"].endswith(".pdf")
    assert data["stages"]["export"]["status"] == "ok"
