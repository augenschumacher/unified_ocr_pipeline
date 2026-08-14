"""Translate common technical failures into user-facing guidance."""

from __future__ import annotations


def friendly_error_message(error: BaseException | str, *, context: str = "") -> str:
    raw = str(error)
    lower = raw.lower()
    prefix = f"{context}\n\n" if context else ""

    if "winerror 145" in lower or "directory is not empty" in lower or "verzeichnis ist nicht leer" in lower:
        return (
            prefix
            + "Der Ordner konnte nicht vollstaendig ersetzt werden, weil Windows noch Dateien darin festhaelt.\n\n"
            "Bitte Unified OCR und das Setup vollstaendig schliessen, kurz warten und die Installation erneut starten. "
            "Falls Antivirus oder Explorer den Ordner blockiert, starte Windows einmal neu."
        )
    if "winerror 5" in lower or "access is denied" in lower or "zugriff verweigert" in lower:
        return (
            prefix
            + "Windows hat den Zugriff verweigert.\n\n"
            "Bitte pruefe die Ordnerrechte oder starte das Setup bzw. die App mit passenden Rechten."
        )
    if "tesseract" in lower and ("not found" in lower or "nicht gefunden" in lower):
        return (
            prefix
            + "Tesseract OCR wurde nicht gefunden.\n\n"
            "Fuehre den Systemcheck aus oder installiere Tesseract ueber den Installer/WinGet."
        )
    if "qpdf" in lower and ("not found" in lower or "nicht gefunden" in lower):
        return (
            prefix
            + "QPDF wurde nicht gefunden.\n\n"
            "QPDF ist fuer OCRmyPDF wichtig. Fuehre den Systemcheck aus oder installiere QPDF ueber den Installer/WinGet."
        )
    if "ghostscript" in lower or "gswin" in lower:
        return (
            prefix
            + "Ghostscript wurde nicht gefunden oder konnte nicht gestartet werden.\n\n"
            "Fuehre den Systemcheck aus oder installiere Ghostscript ueber den Installer/WinGet."
        )
    if "no space" in lower or "not enough space" in lower or "nicht genuegend speicher" in lower:
        return (
            prefix
            + "Es ist nicht genug freier Speicherplatz vorhanden.\n\n"
            "Leere Speicherplatz auf dem Ziel-Laufwerk oder waehle einen anderen Speicherort."
        )

    return prefix + raw
