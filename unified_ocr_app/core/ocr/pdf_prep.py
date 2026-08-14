"""Safe OCRmyPDF preparation helpers.

The default OCR mode is "auto": OCRmyPDF receives "--skip-text" and OCRs only
pages without an existing text layer. Born-digital pages therefore keep their
vector content and native text instead of being rasterized by a blanket
"--force-ocr". "redo" and "force" remain explicit expert choices.

The packaged Windows executable may not expose an ocrmypdf console script. In
that case the same option contract is passed to the bundled Python API.
"""

from __future__ import annotations

import re
import os
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Literal


OCRMode = Literal["auto", "redo", "force"]
OCR_MODES = frozenset({"auto", "redo", "force"})
OCR_OUTPUT_TYPES = frozenset({"pdfa", "pdfa-1", "pdfa-2", "pdfa-3", "pdf"})


def _timeout_from_environment(name: str, default: float) -> float:
    """Read a timeout override; 0 or negative disables the limit."""
    try:
        value = float(os.environ.get(name, "").strip())
    except (TypeError, ValueError):
        return default
    return value


# Grosszuegig gewaehlt: ein mehrhundertseitiger Scan darf lange laufen, ein
# haengender Tesseract-/Ghostscript-Kindprozess aber nicht ewig blockieren.
OCRMYPDF_TIMEOUT_SECONDS = _timeout_from_environment("UNIFIED_OCR_OCRMYPDF_TIMEOUT", 5400.0)


def get_ocrmypdf_command() -> list[str]:
    """Return the OCRmyPDF CLI command when a usable entrypoint exists."""
    exe = shutil.which("ocrmypdf")
    if getattr(sys, "frozen", False):
        return [exe] if exe else []
    return [exe] if exe else [sys.executable, "-m", "ocrmypdf"]


def normalize_ocr_mode(mode: str | None) -> OCRMode:
    """Validate an OCR behavior mode and return its canonical spelling."""
    normalized = str(mode or "auto").strip().lower()
    if normalized not in OCR_MODES:
        choices = ", ".join(sorted(OCR_MODES))
        raise ValueError(f"Unbekannter OCR-Modus {mode!r}; erlaubt: {choices}")
    return normalized  # type: ignore[return-value]


def normalize_ocr_languages(languages: str | Sequence[str] | None) -> tuple[str, ...]:
    """Return validated, de-duplicated Tesseract language identifiers."""
    if languages is None:
        candidates = ["deu"]
    elif isinstance(languages, str):
        candidates = re.split(r"[+,;\s]+", languages)
    else:
        candidates = []
        for value in languages:
            candidates.extend(re.split(r"[+,;\s]+", str(value)))

    result = []
    for candidate in candidates:
        language = candidate.strip()
        if not language:
            continue
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", language):
            raise ValueError(f"Ungueltiger Tesseract-Sprachcode: {language!r}")
        if language not in result:
            result.append(language)
    if not result:
        raise ValueError("Mindestens eine OCR-Sprache muss angegeben werden.")
    return tuple(result)


def list_installed_tesseract_languages(tesseract_command: str | None = None) -> tuple[str, ...]:
    """Return language packs reported by the active Tesseract installation.

    An empty tuple means that availability could not be determined.  It does
    not mean that no languages are installed, which is important for packaged
    OCRmyPDF runtimes that hide their internal Tesseract executable.
    """
    executable = tesseract_command or shutil.which("tesseract")
    if not executable:
        return ()
    try:
        result = subprocess.run(
            [str(executable), "--list-langs"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return ()
    if result.returncode != 0:
        return ()
    lines = f"{result.stdout}\n{result.stderr}".splitlines()
    languages = []
    for raw_line in lines:
        language = raw_line.strip().lstrip("\ufeff")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", language):
            continue
        if language.casefold() in {"list", "languages", "available"}:
            continue
        if language not in languages:
            languages.append(language)
    return tuple(sorted(languages, key=str.casefold))


def resolve_ocr_languages(
    languages: str | Sequence[str] | None,
    installed_languages: Sequence[str] | None = None,
    *,
    strict: bool = False,
    fallback_order: Sequence[str] = ("deu", "eng"),
) -> dict:
    """Resolve requested OCR languages against installed Tesseract data.

    Missing optional languages are reported and removed.  If none of the
    requested packs exists, a deterministic installed fallback is selected in
    non-strict mode.  When the packaged runtime cannot report its packs, the
    syntactically validated request is retained and the uncertainty is exposed
    to diagnostics rather than guessed away.
    """
    requested = normalize_ocr_languages(languages)
    if installed_languages is None:
        detected = list_installed_tesseract_languages()
    elif installed_languages:
        detected = normalize_ocr_languages(installed_languages)
    else:
        detected = ()
    warnings: list[str] = []
    if not detected:
        warnings.append(
            "Installierte Tesseract-Sprachen konnten nicht ermittelt werden; "
            "die angeforderten Sprachen werden ungeprüft verwendet."
        )
        return {
            "requested": list(requested),
            "available": [],
            "effective": list(requested),
            "missing": [],
            "fallback_used": False,
            "detection_available": False,
            "warnings": warnings,
        }

    available_lookup = {language.casefold(): language for language in detected}
    effective = [available_lookup[value.casefold()] for value in requested if value.casefold() in available_lookup]
    missing = [value for value in requested if value.casefold() not in available_lookup]
    if missing and strict:
        raise RuntimeError(
            "Nicht installierte Tesseract-Sprachen: " + ", ".join(missing)
        )
    if missing:
        warnings.append(
            "Nicht installierte OCR-Sprachen wurden ausgelassen: " + ", ".join(missing)
        )

    fallback_used = False
    if not effective:
        fallback = next(
            (
                available_lookup[language.casefold()]
                for language in fallback_order
                if language.casefold() in available_lookup
            ),
            None,
        )
        if fallback is None:
            fallback = next(
                (
                    language
                    for language in detected
                    if language.casefold() not in {"osd", "equ"}
                ),
                None,
            )
        if fallback is None:
            raise RuntimeError(
                "Keine nutzbare Tesseract-Textsprache ist installiert."
            )
        effective = [fallback]
        fallback_used = True
        warnings.append(
            f"Keine angeforderte OCR-Sprache ist installiert; Fallback '{fallback}' wird verwendet."
        )

    return {
        "requested": list(requested),
        "available": list(detected),
        "effective": effective,
        "missing": missing,
        "fallback_used": fallback_used,
        "detection_available": True,
        "warnings": warnings,
    }


def inspect_pdf_page_content(
    pdf_path: Path,
    *,
    hybrid_image_ratio: float = 0.20,
    minimum_text_characters: int = 1,
) -> dict:
    """Identify pages at risk of being skipped by OCRmyPDF ``--skip-text``.

    A page containing both meaningful digital text and a substantial raster
    image is potentially hybrid (for example, a digital letterhead over a scan).
    OCRmyPDF's auto mode preserves such a page wholesale, so the result is a
    blocking review signal rather than a reason to rasterize good vector data
    automatically.
    """
    try:
        import fitz
    except ImportError:
        return {
            "available": False,
            "page_count": 0,
            "hybrid_pages": [],
            "pages": [],
            "warning": "PDF-Seitenabdeckung konnte ohne PyMuPDF nicht geprüft werden.",
        }

    pages = []
    hybrid_pages = []
    with fitz.open(Path(pdf_path)) as document:
        for page_number, page in enumerate(document, start=1):
            text = page.get_text("text") or ""
            text_characters = sum(character.isalnum() for character in text)
            page_area = max(float(page.rect.width * page.rect.height), 1.0)
            image_area = 0.0
            image_count = 0
            seen_rectangles: set[tuple[float, float, float, float]] = set()
            for image in page.get_images(full=True):
                try:
                    rectangles = page.get_image_rects(image[0])
                except Exception:
                    rectangles = []
                for rectangle in rectangles:
                    clipped = rectangle & page.rect
                    key = tuple(round(value, 2) for value in (clipped.x0, clipped.y0, clipped.x1, clipped.y1))
                    if key in seen_rectangles or clipped.is_empty:
                        continue
                    seen_rectangles.add(key)
                    image_count += 1
                    image_area += max(0.0, float(clipped.width * clipped.height))
            image_ratio = min(1.0, image_area / page_area)
            is_hybrid = (
                text_characters >= int(minimum_text_characters)
                and image_ratio >= float(hybrid_image_ratio)
            )
            if is_hybrid:
                hybrid_pages.append(page_number)
            pages.append(
                {
                    "page": page_number,
                    "text_characters": text_characters,
                    "image_count": image_count,
                    "image_area_ratio": round(image_ratio, 3),
                    "hybrid_risk": is_hybrid,
                }
            )
    return {
        "available": True,
        "page_count": len(pages),
        "hybrid_pages": hybrid_pages,
        "pages": pages,
    }


def ocrmypdf_mode_cli_args(mode: str | None = "auto") -> list[str]:
    """Translate the public mode contract to mutually exclusive CLI flags."""
    normalized = normalize_ocr_mode(mode)
    if normalized == "auto":
        return ["--skip-text"]
    if normalized == "redo":
        return ["--redo-ocr"]
    return ["--force-ocr"]


def ocrmypdf_mode_api_options(mode: str | None = "auto") -> dict[str, bool]:
    """Translate the public mode contract to mutually exclusive API options."""
    normalized = normalize_ocr_mode(mode)
    return {
        {
            "auto": "skip_text",
            "redo": "redo_ocr",
            "force": "force_ocr",
        }[normalized]: True
    }


def _run_ocrmypdf_api(input_path: Path, output_pdf: Path, **kwargs) -> None:
    try:
        import ocrmypdf
    except ImportError as exc:
        raise RuntimeError(
            "OCRmyPDF ist weder als CLI noch als gebuendeltes Python-Modul verfuegbar."
        ) from exc

    try:
        ocrmypdf.ocr(str(input_path), str(output_pdf), **kwargs)
    except Exception as exc:
        raise RuntimeError(f"OCRmyPDF fehlgeschlagen: {exc}") from exc


def run_image_to_pdf(image_path: Path, output_pdf: Path) -> str:
    """Convert an image to an image-only PDF without premature OCR.

    OCR language, rotation and deskew decisions belong to the subsequent
    :func:`run_ocrmypdf` call.  Using OCRmyPDF itself as the converter can add
    a default-language text layer which ``auto`` mode would then correctly
    preserve and skip, producing the wrong language result.
    """
    try:
        from PIL import Image, ImageOps, ImageSequence
    except ImportError as exc:
        raise RuntimeError("Pillow wird für die Bild-zu-PDF-Konvertierung benötigt.") from exc

    image_path = Path(image_path)
    output_pdf = Path(output_pdf)
    output_pdf.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_pdf.with_name(f".{output_pdf.name}.image.tmp.pdf")
    frames = []
    try:
        with Image.open(image_path) as image:
            for raw_frame in ImageSequence.Iterator(image):
                frame = ImageOps.exif_transpose(raw_frame.copy())
                if frame.mode in ("RGBA", "LA") or (frame.mode == "P" and "transparency" in frame.info):
                    rgba = frame.convert("RGBA")
                    background = Image.new("RGB", rgba.size, "white")
                    background.paste(rgba, mask=rgba.getchannel("A"))
                    rgba.close()
                    frame.close()
                    frame = background
                elif frame.mode != "RGB":
                    converted = frame.convert("RGB")
                    frame.close()
                    frame = converted
                frames.append(frame)
        if not frames:
            raise RuntimeError("Keine lesbare Bildseite gefunden.")
        frames[0].save(
            temporary,
            "PDF",
            resolution=300.0,
            quality=95,
            save_all=len(frames) > 1,
            append_images=frames[1:],
        )
        os.replace(temporary, output_pdf)
        return ""
    except Exception as exc:
        raise RuntimeError(f"Bild-zu-PDF fehlgeschlagen: {exc}") from exc
    finally:
        temporary.unlink(missing_ok=True)
        for frame in frames:
            try:
                frame.close()
            except Exception:
                pass


def run_ocrmypdf(
    input_pdf: Path,
    output_pdf: Path,
    sidecar_txt: Path,
    *,
    mode: OCRMode | str = "auto",
    languages: str | Sequence[str] = ("deu",),
    deskew: bool = True,
    rotate_pages: bool = True,
    rotate_pages_threshold: float = 7,
    output_type: str = "pdfa-2",
    timeout: float | None = None,
) -> str:
    """Run OCRmyPDF with an archival-safe, configurable preflight policy.

    Modes:
        auto: preserve pages with text and OCR only image-only pages.
        redo: replace an existing OCR layer when it is known to be defective.
        force: rasterize/re-OCR every page; use only as an explicit repair mode.

    languages accepts "deu", "deu+eng" or ("deu", "eng"). CLI and Python-API
    execution receive equivalent options.

    timeout caps the CLI run so that a wedged Tesseract or Ghostscript child
    process cannot stall the pipeline forever.  It defaults to
    OCRMYPDF_TIMEOUT_SECONDS and is generous enough for large scans.
    """
    normalized_mode = normalize_ocr_mode(mode)
    normalized_languages = normalize_ocr_languages(languages)
    normalized_output_type = str(output_type or "pdfa-2").strip().lower()
    if normalized_output_type not in OCR_OUTPUT_TYPES:
        raise ValueError(
            f"Ungültiger OCR-Ausgabetyp {output_type!r}; erlaubt: "
            + ", ".join(sorted(OCR_OUTPUT_TYPES))
        )
    threshold = float(rotate_pages_threshold)
    if threshold < 0:
        raise ValueError("rotate_pages_threshold darf nicht negativ sein.")

    common_api_options = {
        "deskew": bool(deskew),
        "rotate_pages": bool(rotate_pages),
        "language": list(normalized_languages),
        "sidecar": str(sidecar_txt),
        "output_type": normalized_output_type,
        **ocrmypdf_mode_api_options(normalized_mode),
    }
    if rotate_pages:
        common_api_options["rotate_pages_threshold"] = threshold

    cmd = get_ocrmypdf_command()
    if not cmd:
        _run_ocrmypdf_api(input_pdf, output_pdf, **common_api_options)
        return (
            sidecar_txt.read_text(encoding="utf-8", errors="replace")
            if sidecar_txt.exists()
            else ""
        )

    cmd = cmd + ocrmypdf_mode_cli_args(normalized_mode)
    cmd.extend(["--output-type", normalized_output_type])
    if deskew:
        cmd.append("--deskew")
    if rotate_pages:
        cmd.extend(["--rotate-pages", "--rotate-pages-threshold", f"{threshold:g}"])
    cmd.extend(
        [
            "-l",
            "+".join(normalized_languages),
            "--sidecar",
            str(sidecar_txt),
            str(input_pdf),
            str(output_pdf),
        ]
    )
    effective_timeout = OCRMYPDF_TIMEOUT_SECONDS if timeout is None else timeout
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            # OCRmyPDF meldet Pfade und Fehler in UTF-8.  Ohne diese Vorgabe
            # dekodiert Windows mit der ANSI-Codepage und zerlegt Umlaute oder
            # bricht mit UnicodeDecodeError ab.
            encoding="utf-8",
            errors="replace",
            timeout=effective_timeout if effective_timeout and effective_timeout > 0 else None,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"OCRmyPDF hat das Zeitlimit von {effective_timeout:.0f} Sekunden überschritten "
            "und wurde abgebrochen."
        ) from exc
    if result.returncode != 0:
        raise RuntimeError(f"OCRmyPDF fehlgeschlagen: {result.stderr}")
    return (
        sidecar_txt.read_text(encoding="utf-8", errors="replace")
        if sidecar_txt.exists()
        else ""
    )
