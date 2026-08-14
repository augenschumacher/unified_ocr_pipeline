import shutil
from pathlib import Path

import pytest

from core.config import setup_paths
from core.ocr.pdf_prep import (
    list_installed_tesseract_languages,
    run_image_to_pdf,
    run_ocrmypdf,
)
from core.ocr.page_extractor import inject_fused_text_and_metadata, validate_archival_pdf


setup_paths()
_HAS_NATIVE_OCR = bool(
    shutil.which("tesseract")
    and shutil.which("qpdf")
    and (shutil.which("gs") or shutil.which("gswin64c") or shutil.which("gswin32c"))
    and "deu" in list_installed_tesseract_languages()
)


@pytest.mark.skipif(not _HAS_NATIVE_OCR, reason="native OCR/PDF-A toolchain not installed")
def test_real_german_scan_produces_searchable_pdfa_with_critical_values(tmp_path):
    fitz = pytest.importorskip("fitz")
    pytest.importorskip("ocrmypdf")
    from ocrmypdf.pdfa import file_claims_pdfa
    from PIL import Image, ImageDraw, ImageFilter, ImageFont

    font_path = next(
        (
            path
            for path in (
                "C:/Windows/Fonts/arial.ttf",
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            )
            if Path(path).is_file()
        ),
        None,
    )
    if not font_path:
        pytest.skip("no Unicode test font installed")

    name = "M\u00fcller & S\u00f6hne GmbH"
    lines = [
        name,
        "Rechnungsnummer: RE-2026-00417",
        "Rechnungsdatum: 12.07.2026",
        "Gesamtbetrag: 1.234,56 EUR",
        "Vertragskennzeichen: AB-7781-X",
    ]
    image = Image.new("RGB", (2480, 3508), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype(font_path, 64)
    for index, line in enumerate(lines):
        draw.text((220, 300 + index * 150), line, font=font, fill="black")
    # Mild skew, blur and JPEG compression approximate a realistic office scan
    # while staying deterministic across supported OCR toolchains.
    image = image.rotate(
        0.7,
        resample=Image.Resampling.BICUBIC,
        expand=False,
        fillcolor="white",
    ).filter(ImageFilter.GaussianBlur(0.2))
    image_path = tmp_path / "scan.jpg"
    raw_pdf = tmp_path / "raw.pdf"
    output_pdf = tmp_path / "ocr.pdf"
    archival_pdf = tmp_path / "archival.pdf"
    sidecar = tmp_path / "ocr.txt"
    image.save(image_path, dpi=(300, 300), quality=82, subsampling=2)
    image.close()

    run_image_to_pdf(image_path, raw_pdf)
    with fitz.open(raw_pdf) as raw_document:
        assert raw_document[0].get_text().strip() == ""

    text = run_ocrmypdf(
        raw_pdf,
        output_pdf,
        sidecar,
        languages=("deu", "eng"),
        output_type="pdfa-2",
    )
    inject_fused_text_and_metadata(
        output_pdf,
        archival_pdf,
        {1: "Nicht geometrisch belegter LLM-Text 99.999,99 EUR"},
        {"title": "Geprüfte OCR-Stichprobe", "tags": ["Rechnung", "OCR-Test"]},
    )
    with fitz.open(archival_pdf) as document:
        extracted = "\n".join(page.get_text() for page in document)
        positioned_words = document[0].get_text("words")

    for critical_value in (name, "RE-2026-00417", "12.07.2026", "1.234,56 EUR", "AB-7781-X"):
        assert critical_value in text
        assert critical_value in extracted
    assert "99.999,99" not in extracted
    assert file_claims_pdfa(archival_pdf) == {
        "pass": True,
        "output": "pdfa",
        "conformance": "PDF/A-2b",
    }
    postflight = validate_archival_pdf(archival_pdf)
    assert postflight["ok"] is True
    assert postflight["structural_ok"] is True
    assert postflight["full_conformance_validated"] is False
    invoice_label = next(word for word in positioned_words if "Rechnungsnummer" in word[4])
    amount_label = next(word for word in positioned_words if "Gesamtbetrag" in word[4])
    # 220 px at 300 dpi is ~53 pt; the line baselines are ~108 pt and ~216 pt.
    # Broad tolerances allow OCR glyph boxes while still catching a detached or
    # page-wide invisible overlay.
    assert 30 < invoice_label[0] < 100
    assert 80 < invoice_label[1] < 150
    assert 30 < amount_label[0] < 100
    assert 150 < amount_label[1] < 245
