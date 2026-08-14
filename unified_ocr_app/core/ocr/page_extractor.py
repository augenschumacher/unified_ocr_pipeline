"""Page extraction and archival-safe PDF finalization via PyMuPDF.

The finalizer deliberately does *not* put LLM/fused prose into the PDF text
layer. Fused text has no trustworthy word coordinates and would therefore
produce a geometrically false overlay. The OCRmyPDF output is the archival PDF
source: original/vector page objects, links, annotations, images and its OCR
text layer are preserved; only validated document-info metadata may change.

When PyMuPDF is unavailable, extraction returns an empty result and
finalization falls back to a byte-for-byte copy without metadata changes.
"""

import os
import re
import shutil
import tempfile
import time
from datetime import datetime
from pathlib import Path

try:
    import fitz   # PyMuPDF
except ImportError:
    fitz = None


def _replace_with_transient_lock_retry(
    source: Path,
    destination: Path,
    *,
    delays: tuple[float, ...] = (0.05, 0.10, 0.20, 0.40),
) -> None:
    """Atomically replace a file after short-lived Windows handles close."""
    for attempt in range(len(delays) + 1):
        try:
            os.replace(source, destination)
            return
        except OSError as exc:
            transient_lock = isinstance(exc, PermissionError) or getattr(
                exc, "winerror", None
            ) in {5, 32}
            if not transient_lock or attempt >= len(delays):
                raise
            time.sleep(delays[attempt])


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
    pages: dict[int, list[dict]] = {}
    # Das Dokument muss auch bei einem Fehler mitten im Lauf geschlossen werden.
    # Ein offener PyMuPDF-Handle haelt die PDF unter Windows gesperrt, wodurch
    # anschliessendes Verschieben oder Aufraeumen des Arbeitsordners scheitert.
    with fitz.open(pdf_path) as doc:
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
    return pages


def extract_pages_as_images(pdf_path: Path, output_dir: Path, dpi: int = 220) -> list[Path]:
    """
    Rendert jede PDF-Seite als PNG-Datei in einer VLM-tauglichen Auflösung.

    Returns:
        Geordnete Liste der PNG-Pfade (page_0.png, page_1.png, …)
        Leere Liste wenn PyMuPDF nicht verfügbar.
    """
    if not fitz:
        return []
    dpi = max(150, min(int(dpi), 400))
    paths = []
    # Bricht das Rendern einer Seite ab (z. B. volle Platte), darf der Handle
    # nicht offen bleiben; sonst blockiert er das Loeschen des Arbeitsordners.
    with fitz.open(pdf_path) as doc:
        for i in range(len(doc)):
            pix  = doc[i].get_pixmap(dpi=dpi, alpha=False)
            img  = output_dir / f"page_{i}.png"
            pix.save(str(img))
            paths.append(img)
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
    with fitz.open(pdf_path) as doc:
        texts = {i + 1: doc[i].get_text("text") for i in range(len(doc))}
    return texts


def _safe_metadata_text(value) -> str:
    """Return a compact PDF-info string without control characters."""
    if isinstance(value, (list, tuple, set)):
        value = "; ".join(str(item) for item in value if str(item).strip())
    text = str(value or "")
    text = "".join(
        char if char in "\t\n\r" or ord(char) >= 32 else " "
        for char in text
    )
    return re.sub(r"\s+", " ", text).strip()[:4096]


def _pdf_creation_date(value) -> str | None:
    """Convert common document-date forms to a conservative PDF date value."""
    raw = _safe_metadata_text(value)
    if not raw:
        return None
    pdf_date = re.fullmatch(
        r"D:(\d{4})(\d{2})?(\d{2})?(\d{2})?(\d{2})?(\d{2})?"
        r"(?:(Z)|([+-])(\d{2})'?(\d{2})'?)?",
        raw,
    )
    if pdf_date:
        (
            year,
            month,
            day,
            hour,
            minute,
            second,
            _utc,
            _sign,
            zone_hour,
            zone_minute,
        ) = pdf_date.groups()
        try:
            datetime(
                int(year),
                int(month or 1),
                int(day or 1),
                int(hour or 0),
                int(minute or 0),
                int(second or 0),
            )
        except ValueError:
            return None
        if zone_hour and (int(zone_hour) > 23 or int(zone_minute or 0) > 59):
            return None
        return raw

    candidate = raw[:10]
    for date_format in ("%Y-%m-%d", "%d.%m.%Y", "%d-%m-%Y"):
        try:
            parsed = datetime.strptime(candidate, date_format)
        except ValueError:
            continue
        return f"D:{parsed:%Y%m%d}000000"
    return None


def _pdf_date_to_xmp(value) -> str | None:
    pdf_value = _pdf_creation_date(value)
    if not pdf_value:
        return None
    match = re.fullmatch(
        r"D:(\d{4})(\d{2})?(\d{2})?(\d{2})?(\d{2})?(\d{2})?"
        r"(?:(Z)|([+-])(\d{2})'?(\d{2})'?)?",
        pdf_value,
    )
    if not match:
        return None
    year, month, day, hour, minute, second, utc, sign, zone_hour, zone_minute = match.groups()
    value = (
        f"{year}-{month or '01'}-{day or '01'}T"
        f"{hour or '00'}:{minute or '00'}:{second or '00'}"
    )
    if utc:
        return value + "Z"
    if sign and zone_hour:
        return value + f"{sign}{zone_hour}:{zone_minute or '00'}"
    return value


def _metadata_updates(metadata: dict | None) -> dict[str, str]:
    """Map application metadata to safe, non-destructive PDF-info updates.

    ``document_date``/legacy ``date`` describe the record, not the creation of
    the PDF file.  They intentionally remain in the archival sidecar and never
    overwrite PDF ``CreationDate``.  Only an explicitly named PDF creation
    timestamp may update that technical field.
    """
    metadata = metadata if isinstance(metadata, dict) else {}
    updates: dict[str, str] = {}
    for source_key, pdf_key in (
        ("title", "title"),
        ("author", "author"),
        ("subject", "subject"),
        ("creator", "creator"),
    ):
        value = _safe_metadata_text(metadata.get(source_key))
        if value:
            updates[pdf_key] = value

    keywords = _safe_metadata_text(metadata.get("tags") or metadata.get("keywords"))
    if keywords:
        updates["keywords"] = keywords

    creation_date = _pdf_creation_date(
        metadata.get("pdf_creation_date")
        or metadata.get("creationDate")
        or metadata.get("creation_date")
    )
    if creation_date:
        updates["creationDate"] = creation_date
    return updates


def _update_metadata_incrementally(
    pdf_path: Path,
    metadata: dict | None,
    *,
    require_backend: bool = False,
    replace_descriptive_fields: bool = False,
) -> bool:
    """Synchronize PDF Info and XMP metadata without touching page content.

    OCRmyPDF's PDF/A output contains XMP metadata.  Updating only ``/Info``
    would create a PDF/A metadata mismatch, so pikepdf updates both views in a
    single rewrite.  If pikepdf is unavailable, metadata enrichment is skipped
    and the archival PDF stays byte-preserved; the full metadata remains in the
    JSON sidecar and manifest.
    """
    updates = _metadata_updates(metadata)
    replace_fields: set[str] = set()
    if replace_descriptive_fields and isinstance(metadata, dict):
        # Human review is authoritative for these descriptive fields.  Empty
        # values deliberately clear an earlier machine-generated value instead
        # of leaving XMP/DocInfo inconsistent with the manifest.
        updates["title"] = _safe_metadata_text(metadata.get("title"))
        updates["subject"] = _safe_metadata_text(metadata.get("subject"))
        updates["keywords"] = _safe_metadata_text(
            metadata.get("tags") or metadata.get("keywords")
        )
        replace_fields.update({"title", "subject", "keywords"})
    if not updates:
        return False
    try:
        import pikepdf
    except ImportError as exc:
        if require_backend:
            raise RuntimeError(
                "PDF-Metadaten können ohne pikepdf nicht revisionssicher synchronisiert werden."
            ) from exc
        return False

    rewritten = pdf_path.with_name(f".{pdf_path.name}.metadata.pdf")
    try:
        with pikepdf.open(pdf_path) as pdf:
            docinfo = {
                str(key).lstrip("/"): _safe_metadata_text(value)
                for key, value in pdf.docinfo.items()
            }
            merged = {
                "title": docinfo.get("Title", ""),
                "author": docinfo.get("Author", ""),
                "subject": docinfo.get("Subject", ""),
                "keywords": docinfo.get("Keywords", ""),
                "creator": docinfo.get("Creator", ""),
                "producer": docinfo.get("Producer", ""),
                "creationDate": docinfo.get("CreationDate", ""),
                "modDate": docinfo.get("ModDate", ""),
            }
            merged.update(updates)
            with pdf.open_metadata(
                set_pikepdf_as_editor=False,
                update_docinfo=True,
            ) as xmp:
                if merged["title"]:
                    xmp["dc:title"] = merged["title"]
                elif "title" in replace_fields:
                    xmp["dc:title"] = ""
                if merged["author"]:
                    xmp["dc:creator"] = [merged["author"]]
                if merged["subject"]:
                    xmp["dc:description"] = merged["subject"]
                elif "subject" in replace_fields:
                    xmp["dc:description"] = ""
                if merged["keywords"]:
                    xmp["pdf:Keywords"] = merged["keywords"]
                elif "keywords" in replace_fields:
                    xmp["pdf:Keywords"] = ""
                if merged["creator"]:
                    xmp["xmp:CreatorTool"] = merged["creator"]
                if merged["producer"]:
                    xmp["pdf:Producer"] = merged["producer"]
                creation_date = _pdf_date_to_xmp(merged["creationDate"])
                if creation_date:
                    xmp["xmp:CreateDate"] = creation_date
                modification_date = _pdf_date_to_xmp(merged["modDate"])
                if modification_date:
                    xmp["xmp:ModifyDate"] = modification_date
            pdf.save(rewritten)
        _replace_with_transient_lock_retry(rewritten, pdf_path)
        return True
    finally:
        rewritten.unlink(missing_ok=True)


def update_archival_pdf_metadata(
    pdf_path: Path,
    metadata: dict | None,
    *,
    require_backend: bool = False,
) -> bool:
    """Synchronize reviewed descriptive metadata without changing any page.

    This is the review-time counterpart to PDF finalization.  It rewrites XMP
    and document information together, preserves the OCR text layer and page
    geometry, and can require the pikepdf backend for archival workflows.
    """
    pdf_path = Path(pdf_path)
    if not pdf_path.is_file():
        raise FileNotFoundError(f"Archiv-PDF existiert nicht: {pdf_path}")
    return _update_metadata_incrementally(
        pdf_path,
        metadata,
        require_backend=require_backend,
        replace_descriptive_fields=True,
    )


def validate_archival_pdf(pdf_path: Path, *, expected_conformance: str = "PDF/A-2b") -> dict:
    """Run the available post-finalization structural and PDF/A claim checks.

    This is intentionally explicit about scope: pikepdf verifies that the
    rewritten file is structurally readable, while OCRmyPDF's checker validates
    the PDF/A identification metadata.  It is not a substitute for an external
    full veraPDF conformance run, which remains a release/benchmark option.
    """
    pdf_path = Path(pdf_path)
    report = {
        "path": str(pdf_path),
        "expected_conformance": expected_conformance,
        "structural_ok": False,
        "pdfa_claim": {},
        "full_conformance_validated": False,
        "validator_scope": "pikepdf-structure + OCRmyPDF PDF/A identification",
        "ok": False,
    }
    try:
        import pikepdf

        with pikepdf.open(pdf_path) as pdf:
            report["page_count"] = len(pdf.pages)
        report["structural_ok"] = True
    except Exception as exc:
        report["error"] = f"PDF-Strukturprüfung fehlgeschlagen: {exc}"
        return report

    try:
        from ocrmypdf.pdfa import file_claims_pdfa

        claim = file_claims_pdfa(pdf_path)
        report["pdfa_claim"] = claim if isinstance(claim, dict) else {"result": claim}
    except Exception as exc:
        report["error"] = f"PDF/A-Identifikationsprüfung fehlgeschlagen: {exc}"
        return report

    claim = report["pdfa_claim"]
    report["ok"] = bool(
        report["structural_ok"]
        and claim.get("pass") is True
        and claim.get("output") == "pdfa"
        and str(claim.get("conformance") or "").casefold()
        == str(expected_conformance).casefold()
    )
    if not report["ok"]:
        report["error"] = (
            "Finalisiertes PDF weist nicht die erwartete PDF/A-2b-Identifikation auf."
        )
    return report


def inject_fused_text_and_metadata(
    source_pdf: Path,
    target_pdf: Path,
    fused_pages: dict[int, str],
    metadata: dict,
) -> None:
    """Finalize an OCR PDF without changing its pages or OCR text layer.

    source_pdf must be the best archival page representation, normally the
    OCRmyPDF output. fused_pages remains in the signature for compatibility but
    is intentionally not embedded: LLM prose has no reliable word geometry and
    therefore is not an OCR overlay. Export it as TXT/DOCX/JSON instead.

    Finalization is transactional. A temporary copy receives synchronized XMP
    and document-info metadata and atomically replaces target_pdf only after
    metadata handling succeeds.
    Blank or invalid values never erase valid source metadata.
    """
    del fused_pages
    source_pdf = Path(source_pdf)
    target_pdf = Path(target_pdf)
    if not source_pdf.is_file():
        raise FileNotFoundError(f"PDF-Quelle nicht gefunden: {source_pdf}")
    target_pdf.parent.mkdir(parents=True, exist_ok=True)

    if not fitz:
        if source_pdf.resolve() != target_pdf.resolve():
            shutil.copy2(source_pdf, target_pdf)
        return

    file_descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{target_pdf.stem}.",
        suffix=".tmp.pdf",
        dir=target_pdf.parent,
    )
    os.close(file_descriptor)
    temporary_pdf = Path(temp_name)
    try:
        shutil.copy2(source_pdf, temporary_pdf)
        _update_metadata_incrementally(temporary_pdf, metadata)
        os.replace(temporary_pdf, target_pdf)
    finally:
        temporary_pdf.unlink(missing_ok=True)
