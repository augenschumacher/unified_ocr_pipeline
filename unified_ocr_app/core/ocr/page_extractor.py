"""
page_extractor.py – Seitenextraktion und PDF-Textlayer-Injektion via PyMuPDF

Verantwortlichkeiten:
    - PDF-Seiten als PNG-Bilder rendern    (extract_pages_as_images)
    - Eingebetteten OCR-Text lesen         (extract_ocr_text_per_page)
    - Fusionierten Text + Metadaten in PDF (inject_fused_text_and_metadata)

Alle Funktionen degradieren graceful wenn PyMuPDF nicht installiert ist:
    - extract_*  geben [] / {} zurück
    - inject_*   kopiert die Quelldatei unverändert
"""

import shutil
from pathlib import Path

try:
    import fitz   # PyMuPDF
except ImportError:
    fitz = None


def extract_pages_as_images(pdf_path: Path, output_dir: Path) -> list[Path]:
    """
    Rendert jede PDF-Seite als PNG-Datei (150 DPI) in output_dir.

    Returns:
        Geordnete Liste der PNG-Pfade (page_0.png, page_1.png, …)
        Leere Liste wenn PyMuPDF nicht verfügbar.
    """
    if not fitz:
        return []
    doc   = fitz.open(pdf_path)
    paths = []
    for i in range(len(doc)):
        pix  = doc[i].get_pixmap(dpi=150)
        img  = output_dir / f"page_{i}.png"
        pix.save(str(img))
        paths.append(img)
    doc.close()
    return paths


def extract_ocr_text_per_page(pdf_path: Path) -> dict[int, str]:
    """
    Extrahiert den eingebetteten OCR-Text (Sidecar-Layer) pro Seite.

    Returns:
        Dictionary {Seitennummer (1-basiert): Textinhalt}
        Leeres Dict wenn PyMuPDF nicht verfügbar.
    """
    if not fitz:
        return {}
    doc   = fitz.open(pdf_path)
    texts = {i + 1: doc[i].get_text("text") for i in range(len(doc))}
    doc.close()
    return texts


def inject_fused_text_and_metadata(
    source_pdf: Path,
    target_pdf: Path,
    fused_pages: dict[int, str],
    metadata: dict,
) -> None:
    """
    Injiziert den fusionierten Text als unsichtbaren Textlayer (render_mode=3)
    und schreibt PDF-Metadaten (Titel, Keywords, Datum).

    Der Text wird in einer Textbox über die gesamte Seitenfläche gelegt.
    Die Schriftgröße wird automatisch verkleinert bis der Text passt.

    Fallback: Quelldatei wird unverändert kopiert wenn PyMuPDF fehlt.
    """
    if not fitz:
        shutil.copy2(source_pdf, target_pdf)
        return

    doc = fitz.open(source_pdf)

    for i, page in enumerate(doc):
        text = fused_pages.get(i + 1, "")
        if not text:
            continue
        fontsize = 11
        while fontsize > 1:
            rc = page.insert_textbox(
                page.rect, text,
                fontsize    = fontsize,
                fontname    = "helv",
                color       = (0, 0, 0),
                render_mode = 3,      # unsichtbar
                overlay     = True,
            )
            if rc >= 0:
                break
            fontsize -= 1

    # PDF-Metadaten setzen
    meta                 = doc.metadata
    meta["title"]        = metadata.get("title", "")
    meta["subject"]      = metadata.get("subject", "")
    meta["keywords"]     = metadata.get("tags", "")
    meta["creationDate"] = metadata.get("date", "")
    doc.set_metadata(meta)

    doc.save(str(target_pdf), garbage=4, deflate=True)
    doc.close()
