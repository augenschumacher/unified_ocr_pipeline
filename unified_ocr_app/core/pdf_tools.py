# Compatibility-Shim: Weiterleitungen auf das neue core.ocr Paket
# Diese Datei kann gelöscht werden sobald alle externen Referenzen aktualisiert sind.
from core.ocr import (
    get_ocrmypdf_command,
    run_image_to_pdf,
    run_ocrmypdf,
    run_docling_by_page_with_chunks,
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
