"""Command-line runtime diagnostics for Unified OCR."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from core.runtime_paths import default_credentials_path, default_token_path
from core.settings import SettingsManager
from core.system_check import format_system_check, run_system_check


def _load_settings(settings_path: str | None) -> dict:
    manager = SettingsManager(settings_path) if settings_path else SettingsManager()
    return manager.settings


def build_doctor_report(
    *,
    base_dir: str | None = None,
    settings_path: str | None = None,
    credentials_path: str | None = None,
    token_path: str | None = None,
) -> dict:
    settings = _load_settings(settings_path)
    resolved_base_dir = base_dir or settings.get("base_dir") or r"C:\OCR_Workdir"
    resolved_credentials = credentials_path or settings.get("gdrive_credentials_path") or str(default_credentials_path())
    resolved_token = token_path or settings.get("gdrive_token_path") or str(default_token_path())
    return run_system_check(
        resolved_base_dir,
        credentials_path=resolved_credentials,
        token_path=resolved_token,
        ocr_languages=settings.get("ocr_languages", "deu+eng"),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run local runtime diagnostics for Unified OCR.")
    parser.add_argument("--base-dir", help="Override the configured OCR work directory.")
    parser.add_argument("--settings", help="Path to a settings.json file.")
    parser.add_argument("--credentials", help="Path to Google Drive credentials.json.")
    parser.add_argument("--token", help="Path to Google Drive token.json.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    args = parser.parse_args(argv)

    report = build_doctor_report(
        base_dir=args.base_dir,
        settings_path=args.settings,
        credentials_path=args.credentials,
        token_path=args.token,
    )
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(format_system_check(report))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
