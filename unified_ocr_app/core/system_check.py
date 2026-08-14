"""Runtime system checks for release diagnostics and first-run guidance."""

from __future__ import annotations

import importlib.util
import platform
import shutil
import sys
from pathlib import Path


def _command_status(
    name: str,
    *,
    label: str | None = None,
    required: bool = True,
    remediation: str = "",
) -> dict:
    path = shutil.which(name)
    return {
        "name": name,
        "label": label or name,
        "ok": bool(path),
        "required": required,
        "path": path or "",
        "message": "gefunden" if path else "nicht gefunden",
        "remediation": remediation,
    }


def _command_any_status(
    names: list[str],
    *,
    label: str,
    required: bool = True,
    remediation: str = "",
) -> dict:
    for name in names:
        status = _command_status(name, label=label, required=required, remediation=remediation)
        if status["ok"]:
            status["aliases"] = names
            return status
    return {
        "name": names[0],
        "label": label,
        "ok": False,
        "required": required,
        "path": "",
        "message": "nicht gefunden",
        "remediation": remediation,
        "aliases": names,
    }


def _command_or_module_status(command_name: str, module_name: str) -> dict:
    command = _command_status(
        command_name,
        label="OCRmyPDF",
        required=True,
        remediation="Python-Abhaengigkeit installieren oder Release-EXE nutzen.",
    )
    if command["ok"]:
        return command
    spec = importlib.util.find_spec(module_name)
    return {
        "name": command_name,
        "label": "OCRmyPDF",
        "ok": bool(spec),
        "required": True,
        "path": getattr(spec, "origin", "") if spec else "",
        "message": "Python-Modul gefunden" if spec else "nicht gefunden",
        "remediation": "OCRmyPDF ist fuer PDF-OCR erforderlich. Im Installer ist es gebuendelt.",
    }


def _writable_status(path: Path) -> dict:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write_test.tmp"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return {"name": str(path), "ok": True, "required": True, "message": "schreibbar", "remediation": ""}
    except Exception as exc:
        return {
            "name": str(path),
            "ok": False,
            "required": True,
            "message": str(exc),
            "remediation": "Ordnerrechte pruefen oder einen anderen Basisordner waehlen.",
        }


def _disk_status(path: Path, *, minimum_free_gb: int = 5) -> dict:
    try:
        path.mkdir(parents=True, exist_ok=True)
        free_gb = round(shutil.disk_usage(path).free / (1024 ** 3), 1)
        return {
            "name": str(path),
            "ok": free_gb >= minimum_free_gb,
            "free_gb": free_gb,
            "minimum_free_gb": minimum_free_gb,
            "message": f"{free_gb} GB frei",
            "remediation": f"Mindestens {minimum_free_gb} GB frei halten; fuer Ollama-Modelle deutlich mehr.",
        }
    except Exception as exc:
        return {
            "name": str(path),
            "ok": False,
            "free_gb": 0,
            "minimum_free_gb": minimum_free_gb,
            "message": str(exc),
            "remediation": "Speicherort pruefen.",
        }


def _winget_status() -> dict:
    return _command_status(
        "winget",
        label="WinGet",
        required=False,
        remediation="Windows App Installer/WinGet installieren oder Abhaengigkeiten manuell installieren.",
    )


def _ollama_status() -> dict:
    return _command_status(
        "ollama",
        label="Ollama",
        required=False,
        remediation="Nur fuer lokale KI-Modelle erforderlich. Installation optional.",
    )


def run_system_check(
    base_dir: str | Path,
    *,
    credentials_path: str | None = None,
    token_path: str | None = None,
    ocr_languages: str | list[str] | tuple[str, ...] | None = None,
) -> dict:
    try:
        from core.config import setup_paths
        setup_paths()
    except Exception:
        pass

    base = Path(base_dir)
    python_ok = sys.version_info[:2] == (3, 10)
    ghostscript = (
        _command_any_status(
            ["gswin64c", "gswin32c", "gs"],
            label="Ghostscript",
            remediation="Per Installer/WinGet installieren: ArtifexSoftware.GhostScript",
        )
        if platform.system().lower() == "windows"
        else _command_status("gs", label="Ghostscript", remediation="Ghostscript ueber Paketmanager installieren.")
    )
    try:
        from core.ocr.pdf_prep import resolve_ocr_languages

        language_preflight = resolve_ocr_languages(ocr_languages or "deu+eng")
        language_preflight["ok"] = bool(language_preflight.get("effective"))
    except Exception as exc:
        language_preflight = {
            "requested": [str(ocr_languages or "deu+eng")],
            "available": [],
            "effective": [],
            "missing": [],
            "fallback_used": False,
            "detection_available": False,
            "warnings": [str(exc)],
            "ok": False,
        }
    checks = {
        "python": {
            "ok": python_ok,
            "required": True,
            "version": platform.python_version(),
            "message": "Python 3.10 empfohlen" if not python_ok else "ok",
            "remediation": "Release-EXE nutzen oder Python 3.10 installieren.",
        },
        "commands": [
            _command_status(
                "tesseract",
                label="Tesseract OCR",
                remediation="Per Installer/WinGet installieren: UB-Mannheim.TesseractOCR",
            ),
            _command_or_module_status("ocrmypdf", "ocrmypdf"),
            _command_status(
                "qpdf",
                label="QPDF",
                remediation="Per Installer/WinGet installieren: QPDF.QPDF",
            ),
            ghostscript,
        ],
        "optional_commands": [
            _winget_status(),
            _ollama_status(),
        ],
        "directories": [
            _writable_status(base),
            _writable_status(base / "consume"),
            _writable_status(base / "final"),
            _writable_status(base / "logs"),
        ],
        "disk": [
            _disk_status(base, minimum_free_gb=5),
        ],
        "google_drive": {
            "credentials_exists": bool(credentials_path and Path(credentials_path).exists()),
            "token_exists": bool(token_path and Path(token_path).exists()),
        },
        "ocr_languages": language_preflight,
    }
    required_groups = [checks["commands"], checks["directories"], checks["disk"]]
    blocking_items = [
        item
        for group in required_groups
        for item in group
        if item.get("required", True) and not item.get("ok")
    ]
    if not checks["python"]["ok"]:
        blocking_items.insert(0, checks["python"])
    if not language_preflight.get("ok"):
        blocking_items.append({
            "name": "ocr_languages",
            "label": "Tesseract-Sprachpakete",
            "ok": False,
            "required": True,
            "message": "; ".join(language_preflight.get("warnings") or []),
            "remediation": "Mindestens ein passendes Tesseract-Sprachpaket installieren.",
        })
    checks["blocking_issues"] = blocking_items
    checks["ok"] = not blocking_items
    return checks


def _status_symbol(ok: bool) -> str:
    return "OK" if ok else "FEHLT"


def format_system_check(checks: dict) -> str:
    lines = [
        f"Gesamtstatus: {'OK' if checks.get('ok') else 'Achtung'}",
        f"Python: {checks['python']['version']} - {checks['python']['message']}",
    ]
    if checks.get("blocking_issues"):
        lines.extend(["", "Was zu tun ist:"])
        for item in checks["blocking_issues"]:
            label = item.get("label") or item.get("name")
            remediation = item.get("remediation") or "Bitte Komponente pruefen."
            lines.append(f"- {label}: {remediation}")

    lines.extend(["", "Pflichtkomponenten:"])
    for item in checks.get("commands", []):
        label = item.get("label") or item.get("name")
        lines.append(f"- {_status_symbol(item.get('ok'))} {label}: {item['message']} {item.get('path', '')}".rstrip())

    language_preflight = checks.get("ocr_languages", {})
    lines.append(
        "- OCR-Sprachen: angefordert "
        + "+".join(language_preflight.get("requested") or [])
        + "; wirksam "
        + "+".join(language_preflight.get("effective") or [])
    )
    for warning in language_preflight.get("warnings") or []:
        lines.append(f"  Hinweis: {warning}")

    lines.append("")
    lines.append("Optionale Komponenten:")
    for item in checks.get("optional_commands", []):
        label = item.get("label") or item.get("name")
        lines.append(f"- {_status_symbol(item.get('ok'))} {label}: {item['message']} {item.get('path', '')}".rstrip())

    lines.append("")
    lines.append("Ordner:")
    for item in checks.get("directories", []):
        lines.append(f"- {_status_symbol(item.get('ok'))} {item['name']}: {item['message']}")

    lines.append("")
    lines.append("Speicher:")
    for item in checks.get("disk", []):
        lines.append(f"- {_status_symbol(item.get('ok'))} {item['name']}: {item['message']}")

    drive = checks.get("google_drive", {})
    lines.append("")
    lines.append("Google Drive:")
    lines.append(f"- credentials.json vorhanden: {'ja' if drive.get('credentials_exists') else 'nein'}")
    lines.append(f"- token vorhanden: {'ja' if drive.get('token_exists') else 'nein'}")
    return "\n".join(lines)
