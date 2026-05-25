"""
pdf_prep.py – PDF-Vorbereitung und OCRmyPDF

Verantwortlichkeiten:
    - Bild → PDF Konvertierung    (run_image_to_pdf)
    - OCRmyPDF ausführen          (run_ocrmypdf)
    - ocrmypdf-Befehl ermitteln   (get_ocrmypdf_command)
"""

import shutil
import subprocess
import sys
from pathlib import Path


def get_ocrmypdf_command() -> list[str]:
    """Gibt den ocrmypdf-Befehl zurück (CLI-Tool bevorzugt, sonst Python-Modul)."""
    exe = shutil.which("ocrmypdf")
    return [exe] if exe else [sys.executable, "-m", "ocrmypdf"]


def run_image_to_pdf(image_path: Path, output_pdf: Path) -> str:
    """Konvertiert eine Bilddatei (PNG/JPG) in eine PDF-Datei und korrigiert die Drehung."""
    cmd    = get_ocrmypdf_command() + [
        "--image-dpi", "300",
        "--rotate-pages",
        "--rotate-pages-threshold", "7",
        str(image_path),
        str(output_pdf)
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"Bild-zu-PDF fehlgeschlagen: {result.stderr}")
    return result.stdout


def run_ocrmypdf(input_pdf: Path, output_pdf: Path, sidecar_txt: Path) -> str:
    """
    Führt OCRmyPDF aus (Deskew, Drehung, deutsche Sprache) und gibt den Sidecar-Text zurück.

    Optionen:
        --force-ocr   Auch bereits-OCR'd PDFs neu verarbeiten
        --deskew      Schräg eingescannte Seiten begradigen
        --rotate-pages Automatische Ausrichtung (Drehung) der Seiten basierend auf dem Text
        --rotate-pages-threshold 7 Niedrigerer Schwellenwert für zuverlässigere Drehung von Kameraaufnahmen
        -l deu        Tesseract-Sprache: Deutsch
        --sidecar     Text-Sidecar parallel zum PDF speichern
    """
    cmd = get_ocrmypdf_command() + [
        "--force-ocr",
        "--deskew",
        "--rotate-pages",
        "--rotate-pages-threshold", "7",
        "-l", "deu",
        "--sidecar", str(sidecar_txt),
        str(input_pdf),
        str(output_pdf),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"OCRmyPDF fehlgeschlagen: {result.stderr}")
    return sidecar_txt.read_text(encoding="utf-8") if sidecar_txt.exists() else ""
