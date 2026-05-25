"""
core.ocr – OCR-Werkzeuge

Paket-Struktur:
    pdf_prep.py        → Datei-Vorbereitung: img2pdf, ocrmypdf
    docling_runner.py  → Docling + HybridChunker
    page_extractor.py  → PyMuPDF: Seitenbilder, OCR-Text, PDF-Textlayer
"""
from .pdf_prep       import get_ocrmypdf_command, run_image_to_pdf, run_ocrmypdf
from .docling_runner import run_docling_by_page_with_chunks
from .page_extractor import (
    extract_pages_as_images,
    extract_ocr_text_per_page,
    inject_fused_text_and_metadata,
)

__all__ = [
    "get_ocrmypdf_command",
    "run_image_to_pdf",
    "run_ocrmypdf",
    "run_docling_by_page_with_chunks",
    "extract_pages_as_images",
    "extract_ocr_text_per_page",
    "inject_fused_text_and_metadata",
]
