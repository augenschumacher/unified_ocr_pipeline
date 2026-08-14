"""Regressionstests gegen offene PyMuPDF-Handles.

Ein offenes fitz-Dokument sperrt die PDF unter Windows (WinError 32).  Der
entscheidende Punkt ist der Zeitpunkt: solange die Ausnahme behandelt wird,
haelt der Traceback das Frame der fehlgeschlagenen Funktion und damit deren
lokales Dokument am Leben.  Genau in diesem Moment raeumt die Pipeline ihr
Arbeitsverzeichnis im finally-Block auf.  Ohne with-Block scheiterte das
rmtree und hinterliess Restordner unter <base>/work.
"""
import shutil

import fitz
import pytest

from core.ocr import page_extractor


def _make_pdf(path, pages=2):
    with fitz.open() as doc:
        for index in range(pages):
            page = doc.new_page(width=300, height=200)
            page.insert_text((40, 80), f"Seite {index + 1}", fontsize=14)
        doc.save(str(path))
    return path


def test_work_dir_removable_while_render_error_is_handled(tmp_path, monkeypatch):
    work_dir = tmp_path / "job_1"
    work_dir.mkdir()
    pdf_path = _make_pdf(work_dir / "ocrmypdf_out.pdf")

    def failing_save(self, filename, *args, **kwargs):
        raise OSError("Datentraeger voll")

    monkeypatch.setattr(fitz.Pixmap, "save", failing_save, raising=False)

    removed = False
    try:
        page_extractor.extract_pages_as_images(pdf_path, work_dir)
    except OSError:
        # Spiegelt den finally-Block von PipelineOrchestrator.process_file:
        # das Aufraeumen laeuft, waehrend der Traceback noch lebt.
        shutil.rmtree(work_dir)
        removed = True

    assert removed, "Renderfehler wurde nicht ausgeloest"
    assert not work_dir.exists()


def test_work_dir_removable_while_block_error_is_handled(tmp_path, monkeypatch):
    work_dir = tmp_path / "job_2"
    work_dir.mkdir()
    pdf_path = _make_pdf(work_dir / "ocrmypdf_out.pdf")

    def failing_get_text(self, *args, **kwargs):
        raise RuntimeError("beschaedigte Seite")

    monkeypatch.setattr(fitz.Page, "get_text", failing_get_text, raising=False)

    removed = False
    try:
        page_extractor.extract_ordered_text_blocks_per_page(pdf_path)
    except RuntimeError:
        shutil.rmtree(work_dir)
        removed = True

    assert removed, "Seitenfehler wurde nicht ausgeloest"
    assert not work_dir.exists()


def test_work_dir_removable_after_successful_extraction(tmp_path):
    work_dir = tmp_path / "job_3"
    work_dir.mkdir()
    pdf_path = _make_pdf(work_dir / "ocrmypdf_out.pdf", pages=3)

    assert len(page_extractor.extract_pages_as_images(pdf_path, work_dir)) == 3
    page_extractor.extract_ocr_text_per_page(pdf_path)

    shutil.rmtree(work_dir)
    assert not work_dir.exists()
