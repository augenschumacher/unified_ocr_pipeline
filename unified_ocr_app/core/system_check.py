"""Runtime system checks for release diagnostics."""

from __future__ import annotations

import platform
import shutil
import sys
import importlib.util
from pathlib import Path


def _command_status(name: str) -> dict:
    path = shutil.which(name)
    return {
        "name": name,
        "ok": bool(path),
        "path": path or "",
        "message": "gefunden" if path else "nicht gefunden",
    }


def _command_or_module_status(command_name: str, module_name: str) -> dict:
    command = _command_status(command_name)
    if command["ok"]:
        return command
    spec = importlib.util.find_spec(module_name)
    return {
        "name": command_name,
        "ok": bool(spec),
        "path": getattr(spec, "origin", "") if spec else "",
        "message": "Python-Modul gefunden" if spec else "nicht gefunden",
    }


def _writable_status(path: Path) -> dict:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write_test.tmp"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return {"name": str(path), "ok": True, "message": "schreibbar"}
    except Exception as exc:
        return {"name": str(path), "ok": False, "message": str(exc)}


def run_system_check(base_dir: str | Path, *, credentials_path: str | None = None, token_path: str | None = None) -> dict:
    try:
        from core.config import setup_paths
        setup_paths()
    except Exception:
        pass

    base = Path(base_dir)
    python_ok = sys.version_info[:2] == (3, 10)
    checks = {
        "python": {
            "ok": python_ok,
            "version": platform.python_version(),
            "message": "Python 3.10 empfohlen" if not python_ok else "ok",
        },
        "commands": [
            _command_status("tesseract"),
            _command_or_module_status("ocrmypdf", "ocrmypdf"),
            _command_status("gswin64c") if platform.system().lower() == "windows" else _command_status("gs"),
        ],
        "directories": [
            _writable_status(base),
            _writable_status(base / "consume"),
            _writable_status(base / "final"),
            _writable_status(base / "logs"),
        ],
        "google_drive": {
            "credentials_exists": bool(credentials_path and Path(credentials_path).exists()),
            "token_exists": bool(token_path and Path(token_path).exists()),
        },
    }
    checks["ok"] = (
        checks["python"]["ok"]
        and all(item["ok"] for item in checks["commands"])
        and all(item["ok"] for item in checks["directories"])
    )
    return checks


def format_system_check(checks: dict) -> str:
    lines = [
        f"Gesamtstatus: {'OK' if checks.get('ok') else 'Achtung'}",
        f"Python: {checks['python']['version']} - {checks['python']['message']}",
        "",
        "Externe Programme:",
    ]
    for item in checks.get("commands", []):
        lines.append(f"- {item['name']}: {item['message']} {item.get('path', '')}".rstrip())
    lines.append("")
    lines.append("Ordner:")
    for item in checks.get("directories", []):
        lines.append(f"- {item['name']}: {item['message']}")
    drive = checks.get("google_drive", {})
    lines.append("")
    lines.append("Google Drive:")
    lines.append(f"- credentials.json vorhanden: {'ja' if drive.get('credentials_exists') else 'nein'}")
    lines.append(f"- token vorhanden: {'ja' if drive.get('token_exists') else 'nein'}")
    return "\n".join(lines)
