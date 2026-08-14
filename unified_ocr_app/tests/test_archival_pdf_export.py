from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import textwrap
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest


fitz = pytest.importorskip("fitz")

from core.ocr import page_extractor
from core.ocr.page_extractor import inject_fused_text_and_metadata, update_archival_pdf_metadata
from core.exporter import DocumentExporter


def test_atomic_pdf_replace_retries_transient_windows_file_lock(monkeypatch, tmp_path):
    source = tmp_path / ".review.pdf.metadata.pdf"
    destination = tmp_path / "review.pdf"
    source.write_bytes(b"new")
    destination.write_bytes(b"old")
    calls: list[tuple[Path, Path]] = []
    delays: list[float] = []

    def replace_with_two_transient_locks(src, dst):
        calls.append((Path(src), Path(dst)))
        if len(calls) < 3:
            error = PermissionError("temporär gesperrt")
            error.winerror = 5
            raise error
        Path(dst).write_bytes(Path(src).read_bytes())
        Path(src).unlink()

    monkeypatch.setattr(page_extractor.os, "replace", replace_with_two_transient_locks)
    monkeypatch.setattr(page_extractor.time, "sleep", delays.append)

    page_extractor._replace_with_transient_lock_retry(source, destination)

    assert len(calls) == 3
    assert delays == [0.05, 0.10]
    assert destination.read_bytes() == b"new"


def _make_vector_pdf(path):
    doc = fitz.open()
    page = doc.new_page(width=420, height=300)
    page.draw_rect(
        fitz.Rect(35, 35, 385, 265),
        color=(0.1, 0.2, 0.8),
        fill=(0.95, 0.95, 1.0),
        width=2,
    )
    page.insert_text((55, 85), "Born-digitaler Vektortext", fontsize=16)
    # This models the real OCRmyPDF contract: searchable source text already
    # has coordinates. It is extractable but invisible in the rendering.
    page.insert_text(
        (55, 125),
        "Bestehender OCR-Layer 4711",
        fontsize=10,
        render_mode=3,
    )
    page.insert_link(
        {
            "kind": fitz.LINK_URI,
            "from": fitz.Rect(50, 145, 260, 170),
            "uri": "https://example.org/archive",
        }
    )
    page.add_text_annot((330, 80), "Archivnotiz")
    doc.set_metadata(
        {
            "title": "Quelltitel",
            "author": "Bestandsbildner",
            "creationDate": "D:20200102000000",
        }
    )
    doc.save(path)
    doc.close()


def _render_hash(page) -> str:
    pixmap = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False, annots=True)
    return hashlib.sha256(pixmap.samples).hexdigest()


def test_final_pdf_preserves_vectors_links_annotations_and_existing_text(tmp_path):
    source = tmp_path / "ocrmypdf_source.pdf"
    target = tmp_path / "final.pdf"
    _make_vector_pdf(source)

    with fitz.open(source) as doc:
        source_text = doc[0].get_text("text")
        source_words = doc[0].get_text("words")
        source_links = [link.get("uri") for link in doc[0].get_links()]
        source_drawings = len(doc[0].get_drawings())
        source_images = doc[0].get_images(full=True)
        source_annotations = len(list(doc[0].annots() or []))
        source_render = _render_hash(doc[0])

    inject_fused_text_and_metadata(
        source,
        target,
        {1: "LLM-Erfindung: 99.999,99 EUR am 31.12.2099"},
        {
            "title": "Archivierter Titel",
            "tags": ["Rechnung", "Bestand A"],
            "date": "2024-07-03",
        },
    )

    with fitz.open(target) as doc:
        page = doc[0]
        assert page.get_text("text") == source_text
        assert page.get_text("words") == source_words
        assert "Bestehender OCR-Layer 4711" in page.get_text("text")
        assert "99.999,99" not in page.get_text("text")
        assert [link.get("uri") for link in page.get_links()] == source_links
        assert len(page.get_drawings()) == source_drawings
        assert source_drawings > 0
        assert page.get_images(full=True) == source_images == []
        assert len(list(page.annots() or [])) == source_annotations == 1
        assert _render_hash(page) == source_render
        assert doc.metadata["title"] == "Archivierter Titel"
        assert doc.metadata["author"] == "Bestandsbildner"
        assert doc.metadata["keywords"] == "Rechnung; Bestand A"
        # A document date is an archival fact, not the technical creation
        # timestamp of the derivative PDF.  It remains in the sidecar and must
        # not overwrite valid source metadata.
        assert doc.metadata["creationDate"].startswith("D:20200102")

    pikepdf = pytest.importorskip("pikepdf")
    with pikepdf.open(target) as pdf:
        with pdf.open_metadata() as xmp:
            assert xmp["dc:title"] == "Archivierter Titel"
            assert xmp["pdf:Keywords"] == "Rechnung; Bestand A"
            assert xmp["xmp:CreateDate"].startswith("2020-01-02")


def test_blank_or_invalid_metadata_does_not_erase_source_values(tmp_path):
    source = tmp_path / "source.pdf"
    target = tmp_path / "target.pdf"
    _make_vector_pdf(source)

    inject_fused_text_and_metadata(
        source,
        target,
        {},
        {
            "title": "",
            "author": None,
            "tags": [],
            "creationDate": "D:20241301000000",
            "date": "unbekannt",
        },
    )

    with fitz.open(target) as doc:
        assert doc.metadata["title"] == "Quelltitel"
        assert doc.metadata["author"] == "Bestandsbildner"
        assert doc.metadata["creationDate"].startswith("D:20200102")


def test_human_review_can_clear_machine_descriptive_pdf_metadata(tmp_path):
    pdf_path = tmp_path / "reviewed.pdf"
    _make_vector_pdf(pdf_path)

    update_archival_pdf_metadata(
        pdf_path,
        {"title": "", "subject": "", "tags": []},
        require_backend=True,
    )

    with fitz.open(pdf_path) as document:
        assert document.metadata["title"] == ""
        assert document.metadata["subject"] == ""
        assert document.metadata["keywords"] == ""
    pikepdf = pytest.importorskip("pikepdf")
    with pikepdf.open(pdf_path) as pdf:
        with pdf.open_metadata() as xmp:
            assert str(xmp.get("dc:title") or "") == ""
            assert str(xmp.get("dc:description") or "") == ""
            assert str(xmp.get("pdf:Keywords") or "") == ""


def test_exporter_separates_archival_pdf_from_fused_text_derivative(tmp_path):
    source = tmp_path / "ocrmypdf_source.pdf"
    _make_vector_pdf(source)
    final_dir = tmp_path / "final"
    final_dir.mkdir()
    messages = []
    exporter = DocumentExporter(
        config=SimpleNamespace(final_dir=final_dir),
        output_format="PDF und TXT",
        docx_mode="Lesbare DOCX",
        save_docx_enabled=False,
        save_json_enabled=False,
        gdrive_enabled=False,
        gdrive_upload_docx=False,
        gdrive_upload_json=False,
        log_callback=messages.append,
    )
    fused = "LLM-Textderivat mit Wert 99.999,99 EUR"

    outputs = exporter.export(
        source,
        {1: fused},
        fused,
        "archivgut",
        {"title": "Archivgut"},
        [],
        {"quality_status": "ok"},
    )

    assert outputs["txt"].read_text(encoding="utf-8") == fused
    with fitz.open(outputs["pdf"]) as doc:
        pdf_text = doc[0].get_text("text")
    assert "Born-digitaler Vektortext" in pdf_text
    assert "99.999,99" not in pdf_text
    assert any("verlustfrei" in message for message in messages)


def test_exporter_never_overwrites_existing_package_and_keeps_one_conflict_stem(tmp_path):
    source = tmp_path / "source.pdf"
    _make_vector_pdf(source)
    final_dir = tmp_path / "final"
    final_dir.mkdir()
    existing_pdf = final_dir / "archivgut.pdf"
    existing_txt = final_dir / "archivgut.txt"
    existing_pdf.write_bytes(b"existing-pdf")
    existing_txt.write_text("existing-text", encoding="utf-8")
    exporter = DocumentExporter(
        config=SimpleNamespace(final_dir=final_dir),
        output_format="PDF und TXT",
        docx_mode="Lesbare DOCX",
        save_docx_enabled=False,
        save_json_enabled=False,
        gdrive_enabled=False,
        gdrive_upload_docx=False,
        gdrive_upload_json=False,
        log_callback=lambda _message: None,
    )

    outputs = exporter.export(
        source,
        {},
        "neuer Text",
        "archivgut",
        {},
        [],
        {"quality_status": "ok"},
    )

    assert existing_pdf.read_bytes() == b"existing-pdf"
    assert existing_txt.read_text(encoding="utf-8") == "existing-text"
    assert exporter.last_final_name == "archivgut_conflict_001"
    assert outputs["pdf"].stem == outputs["txt"].stem == exporter.last_final_name


def test_concurrent_exporters_reserve_distinct_complete_package_names(tmp_path):
    source = tmp_path / "source.pdf"
    _make_vector_pdf(source)
    final_dir = tmp_path / "final"
    final_dir.mkdir()
    barrier = threading.Barrier(2)
    outputs = []
    failures = []

    def slow_copy(source_path, target_path, _pages, _metadata):
        barrier.wait(timeout=5)
        shutil.copy2(source_path, target_path)

    def run_export(text):
        try:
            exporter = DocumentExporter(
                config=SimpleNamespace(final_dir=final_dir),
                output_format="PDF und TXT",
                docx_mode="Lesbare DOCX",
                save_docx_enabled=False,
                save_json_enabled=False,
                gdrive_enabled=False,
                gdrive_upload_docx=False,
                gdrive_upload_json=False,
                log_callback=lambda _message: None,
                inject_pdf_func=slow_copy,
            )
            outputs.append(exporter.export(source, {}, text, "gleich", {}, [], {}))
        except Exception as exc:  # pragma: no cover - asserted below
            failures.append(exc)

    threads = [threading.Thread(target=run_export, args=(text,)) for text in ("eins", "zwei")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not failures
    assert len(outputs) == 2
    stems = {item["pdf"].stem for item in outputs}
    assert stems == {"gleich", "gleich_conflict_001"}
    assert {item["txt"].stem for item in outputs} == stems
    assert {item["txt"].read_text(encoding="utf-8") for item in outputs} == {"eins", "zwei"}


def test_failed_pdf_postflight_publishes_no_partial_package(tmp_path):
    source = tmp_path / "source.pdf"
    _make_vector_pdf(source)
    final_dir = tmp_path / "final"
    final_dir.mkdir()
    exporter = DocumentExporter(
        config=SimpleNamespace(final_dir=final_dir),
        output_format="PDF und TXT",
        docx_mode="Lesbare DOCX",
        save_docx_enabled=False,
        save_json_enabled=True,
        gdrive_enabled=False,
        gdrive_upload_docx=False,
        gdrive_upload_json=False,
        log_callback=lambda _message: None,
        validate_archival_pdf_enabled=True,
        validate_archival_pdf_func=lambda _path: {"ok": False, "error": "invalid"},
    )

    with pytest.raises(RuntimeError, match="invalid"):
        exporter.export(source, {}, "text", "abgelehnt", {}, [], {})

    assert not (final_dir / "abgelehnt.pdf").exists()
    assert not (final_dir / "abgelehnt.txt").exists()
    assert not (final_dir / "begleitdateien" / "abgelehnt_quality_report.json").exists()


def test_successful_postflight_is_written_with_durable_pdf_path(tmp_path):
    source = tmp_path / "source.pdf"
    _make_vector_pdf(source)
    final_dir = tmp_path / "final"
    final_dir.mkdir()
    quality = {}
    exporter = DocumentExporter(
        config=SimpleNamespace(final_dir=final_dir),
        output_format="Nur PDF",
        docx_mode="Lesbare DOCX",
        save_docx_enabled=False,
        save_json_enabled=True,
        gdrive_enabled=False,
        gdrive_upload_docx=False,
        gdrive_upload_json=False,
        log_callback=lambda _message: None,
        validate_archival_pdf_enabled=True,
        validate_archival_pdf_func=lambda path: {"ok": True, "path": str(path)},
    )

    outputs = exporter.export(source, {}, "text", "postflight", {}, [], quality)

    expected_pdf = final_dir / "postflight.pdf"
    report = json.loads(outputs["json"].read_text(encoding="utf-8"))
    assert outputs["pdf"] == expected_pdf
    assert quality["pdf_postflight"]["path"] == str(expected_pdf)
    assert report["pdf_postflight"]["path"] == str(expected_pdf)


def test_next_exporter_recovers_package_after_hard_process_abort(tmp_path):
    final_dir = tmp_path / "final"
    source = tmp_path / "source.bin"
    source.write_bytes(b"source")
    app_root = Path(__file__).resolve().parents[1]
    script = textwrap.dedent(
        """
        import os
        import sys
        from pathlib import Path
        from types import SimpleNamespace

        sys.path.insert(0, sys.argv[1])
        import core.exporter as exporter_module
        from core.exporter import DocumentExporter

        final_dir = Path(sys.argv[2])
        source = Path(sys.argv[3])
        original_publish = exporter_module._publish_without_overwrite
        calls = {"count": 0}

        def abort_after_first(staged, destination):
            original_publish(staged, destination)
            calls["count"] += 1
            if calls["count"] == 1:
                os._exit(91)

        def fake_pdf(_source, target, _pages, _metadata):
            Path(target).write_bytes(b"pdf-content")

        exporter_module._publish_without_overwrite = abort_after_first
        exporter = DocumentExporter(
            config=SimpleNamespace(final_dir=final_dir),
            output_format="PDF und TXT",
            docx_mode="Lesbare DOCX",
            save_docx_enabled=False,
            save_json_enabled=False,
            gdrive_enabled=False,
            gdrive_upload_docx=False,
            gdrive_upload_json=False,
            log_callback=lambda _message: None,
            inject_pdf_func=fake_pdf,
        )
        exporter.export(source, {}, "text-content", "crash", {}, [], {})
        """
    )
    completed = subprocess.run(
        [sys.executable, "-c", script, str(app_root), str(final_dir), str(source)],
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert completed.returncode == 91
    assert (final_dir / "crash.pdf").read_bytes() == b"pdf-content"
    assert not (final_dir / "crash.txt").exists()
    assert list((final_dir / "_export_transactions").glob("*/export_transaction.json"))

    DocumentExporter(
        config=SimpleNamespace(final_dir=final_dir),
        output_format="Nur TXT",
        docx_mode="Lesbare DOCX",
        save_docx_enabled=False,
        save_json_enabled=False,
        gdrive_enabled=False,
        gdrive_upload_docx=False,
        gdrive_upload_json=False,
        log_callback=lambda _message: None,
    )

    assert (final_dir / "crash.pdf").read_bytes() == b"pdf-content"
    assert (final_dir / "crash.txt").read_text(encoding="utf-8") == "text-content"
    assert not list((final_dir / "_export_transactions").glob("*/export_transaction.json"))


def test_pipeline_reconciles_postflight_and_quality_sidecar_after_move(tmp_path):
    pdf = tmp_path / "archive" / "document.pdf"
    sidecar = tmp_path / "archive" / "document_quality_report.json"
    pdf.parent.mkdir()
    pdf.write_bytes(b"pdf")
    sidecar.write_text("{}", encoding="utf-8")
    quality = {"pdf_postflight": {"ok": True, "path": "old/publication/document.pdf"}}

    from core.pipeline import PipelineOrchestrator

    changed = PipelineOrchestrator._reconcile_pdf_postflight_artifacts(
        quality,
        {"pdf": pdf, "json": sidecar},
    )

    assert changed is True
    assert quality["pdf_postflight"]["path"] == str(pdf)
    assert json.loads(sidecar.read_text(encoding="utf-8"))["pdf_postflight"]["path"] == str(pdf)


def test_proof_docx_receives_all_pages_and_page_images(tmp_path):
    source = tmp_path / "source.pdf"
    _make_vector_pdf(source)
    final_dir = tmp_path / "final"
    final_dir.mkdir()
    captured = {}

    def fake_save(text_input, output_path, **kwargs):
        captured["text_input"] = text_input
        captured["image_paths"] = kwargs.get("image_paths")
        Path(output_path).write_bytes(b"docx")
        return Path(output_path)

    exporter = DocumentExporter(
        config=SimpleNamespace(final_dir=final_dir),
        output_format="PDF und DOCX",
        docx_mode="Prüf-DOCX",
        save_docx_enabled=True,
        save_json_enabled=False,
        gdrive_enabled=False,
        gdrive_upload_docx=False,
        gdrive_upload_json=False,
        log_callback=lambda _message: None,
        save_docx_func=fake_save,
    )
    pages = {1: "Text Seite eins", 2: "Text Seite zwei"}
    images = [tmp_path / "page_1.png", tmp_path / "page_2.png"]

    exporter.export(
        source,
        pages,
        "Text Seite eins\n\nText Seite zwei",
        "mehrseitig",
        {},
        images,
        {"quality_status": "review"},
    )

    assert captured["text_input"] == pages
    assert captured["image_paths"] == images
