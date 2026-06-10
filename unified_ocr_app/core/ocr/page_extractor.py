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

import re
import shutil
from pathlib import Path

try:
    import fitz   # PyMuPDF
except ImportError:
    fitz = None


def _clean_text(text: str) -> str:
    return re.sub(r"[ \t]+", " ", str(text or "")).strip()


def _plain_reading_order(blocks: list[dict]) -> list[dict]:
    return sorted(blocks, key=lambda b: (round(float(b["y0"]) / 8), float(b["x0"])))


def order_text_blocks(blocks: list[dict], page_width: float, page_height: float) -> list[dict]:
    """
    Sort text blocks in a human reading order.

    For two-column pages the desired order is usually: full-width header,
    left column top-to-bottom, right column top-to-bottom, full-width footer.
    Single-column pages keep a normal top-to-bottom, left-to-right order.
    """
    cleaned = []
    for block in blocks or []:
        text = _clean_text(block.get("text", ""))
        if not text:
            continue
        item = dict(block)
        item["text"] = text
        cleaned.append(item)

    if len(cleaned) < 4 or page_width <= 0:
        return _plain_reading_order(cleaned)

    full_width_threshold = page_width * 0.68
    body_blocks = [
        b for b in cleaned
        if float(b["x1"]) - float(b["x0"]) < full_width_threshold
    ]
    if len(body_blocks) < 4:
        return _plain_reading_order(cleaned)

    centers = sorted((float(b["x0"]) + float(b["x1"])) / 2 for b in body_blocks)
    gaps = [(centers[i + 1] - centers[i], i) for i in range(len(centers) - 1)]
    largest_gap, gap_index = max(gaps, default=(0, -1))
    if largest_gap < page_width * 0.16:
        return _plain_reading_order(cleaned)

    split_x = (centers[gap_index] + centers[gap_index + 1]) / 2
    left = [b for b in body_blocks if (float(b["x0"]) + float(b["x1"])) / 2 <= split_x]
    right = [b for b in body_blocks if (float(b["x0"]) + float(b["x1"])) / 2 > split_x]
    if len(left) < 2 or len(right) < 2:
        return _plain_reading_order(cleaned)

    body_min_y = min(float(b["y0"]) for b in body_blocks)
    body_max_y = max(float(b["y1"]) for b in body_blocks)
    body_ids = {id(b) for b in body_blocks}
    full_blocks = [b for b in cleaned if id(b) not in body_ids]
    top_full = [b for b in full_blocks if float(b["y1"]) <= body_min_y + page_height * 0.025]
    bottom_full = [b for b in full_blocks if float(b["y0"]) >= body_max_y - page_height * 0.025]
    middle_full = [b for b in full_blocks if b not in top_full and b not in bottom_full]

    return [
        *_plain_reading_order(top_full),
        *sorted(left, key=lambda b: (float(b["y0"]), float(b["x0"]))),
        *sorted(right, key=lambda b: (float(b["y0"]), float(b["x0"]))),
        *_plain_reading_order(middle_full),
        *_plain_reading_order(bottom_full),
    ]


def split_text_into_packets(text: str, target_count: int) -> list[str]:
    """Split final page text into stable paragraph-like packets."""
    text = str(text or "").strip()
    if not text:
        return []
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n+", text) if p.strip()]
    if not paragraphs:
        paragraphs = [line.strip() for line in text.splitlines() if line.strip()]
    if target_count <= 1 or len(paragraphs) <= target_count:
        return paragraphs or [text]

    packets = []
    remaining = list(paragraphs)
    for index in range(target_count):
        slots_left = target_count - index
        if slots_left <= 1:
            packets.append("\n\n".join(remaining))
            break
        chars_left = sum(len(p) for p in remaining)
        target_chars = max(1, chars_left // slots_left)
        current = []
        current_len = 0
        while remaining and (not current or current_len < target_chars):
            part = remaining.pop(0)
            current.append(part)
            current_len += len(part)
        packets.append("\n\n".join(current))
    return [p for p in packets if p.strip()]


def extract_ordered_text_blocks_per_page(pdf_path: Path) -> dict[int, list[dict]]:
    """
    Extract text blocks with coordinates and deterministic reading order.

    The returned dictionaries intentionally contain only JSON-friendly values.
    """
    if not fitz or not Path(pdf_path).exists():
        return {}
    doc = fitz.open(pdf_path)
    pages: dict[int, list[dict]] = {}
    for page_index, page in enumerate(doc):
        raw_blocks = []
        for block in page.get_text("blocks"):
            if len(block) < 7:
                continue
            x0, y0, x1, y1, text, block_no, block_type = block[:7]
            if block_type != 0:
                continue
            cleaned = _clean_text(text)
            if not cleaned:
                continue
            raw_blocks.append({
                "page": page_index + 1,
                "index": int(block_no),
                "x0": float(x0),
                "y0": float(y0),
                "x1": float(x1),
                "y1": float(y1),
                "text": cleaned,
            })
        ordered = order_text_blocks(raw_blocks, float(page.rect.width), float(page.rect.height))
        for order, block in enumerate(ordered):
            block["reading_order"] = order
        pages[page_index + 1] = ordered
    doc.close()
    return pages


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
    blocks = extract_ordered_text_blocks_per_page(pdf_path)
    if blocks:
        return {
            page_num: "\n\n".join(block.get("text", "") for block in page_blocks if block.get("text"))
            for page_num, page_blocks in blocks.items()
        }
    doc = fitz.open(pdf_path)
    texts = {i + 1: doc[i].get_text("text") for i in range(len(doc))}
    doc.close()
    return texts


def _insert_hidden_textbox(page, rect, text: str):
    if not text.strip():
        return
    rect = fitz.Rect(rect)
    line_count = max(1, text.count("\n") + 1)
    fontsize = min(10.0, max(2.4, (rect.height / line_count) * 0.78))
    while fontsize >= 2.0:
        rc = page.insert_textbox(
            rect,
            text,
            fontsize=fontsize,
            fontname="helv",
            color=(0, 0, 0),
            render_mode=3,
            overlay=True,
        )
        if rc >= 0:
            return
        fontsize -= 0.5
    page.insert_text(
        (rect.x0, max(rect.y0 + 2, 2)),
        text,
        fontsize=2,
        fontname="helv",
        color=(0, 0, 0),
        render_mode=3,
        overlay=True,
    )


def _text_packets_for_page(page_rect, fused_text: str, layout_blocks: list[dict]) -> list[tuple]:
    blocks = layout_blocks or []
    if not blocks:
        margin_x = max(18, page_rect.width * 0.04)
        margin_y = max(18, page_rect.height * 0.035)
        return [(fitz.Rect(margin_x, margin_y, page_rect.width - margin_x, page_rect.height - margin_y), fused_text)]

    packets = split_text_into_packets(fused_text, len(blocks))
    if not packets:
        return []
    result = []
    for block, packet in zip(blocks, packets):
        rect = fitz.Rect(block["x0"], block["y0"], block["x1"], block["y1"])
        rect = rect + (-1, -1, 1, 1)
        result.append((rect, packet))
    if len(packets) > len(blocks):
        last_rect = result[-1][0] if result else page_rect
        result.append((last_rect, "\n\n".join(packets[len(blocks):])))
    return result


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

    source = fitz.open(source_pdf)
    layout_blocks_by_page = extract_ordered_text_blocks_per_page(source_pdf)
    doc = fitz.open()

    for i, source_page in enumerate(source):
        page = doc.new_page(width=source_page.rect.width, height=source_page.rect.height)
        # Rebuild pages from a rendered image so the old OCR layer is not duplicated.
        pix = source_page.get_pixmap(dpi=200, alpha=False)
        page.insert_image(page.rect, pixmap=pix)
        text = fused_pages.get(i + 1, "")
        if not text:
            continue
        packets = _text_packets_for_page(page.rect, text, layout_blocks_by_page.get(i + 1, []))
        for rect, packet_text in packets:
            _insert_hidden_textbox(page, rect, packet_text)

    # PDF-Metadaten setzen
    meta                 = source.metadata
    meta["title"]        = metadata.get("title", "")
    meta["subject"]      = metadata.get("subject", "")
    meta["keywords"]     = metadata.get("tags", "")
    meta["creationDate"] = metadata.get("date", "")
    doc.set_metadata(meta)

    doc.save(str(target_pdf), garbage=4, deflate=True)
    doc.close()
    source.close()
