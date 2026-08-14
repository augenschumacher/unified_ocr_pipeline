"""Runtime audit metadata for generated quality reports."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from datetime import datetime, timezone
from functools import lru_cache
from importlib import metadata as importlib_metadata


APP_VERSION = "0.2.0"


@lru_cache(maxsize=1)
def _toolchain_versions() -> dict:
    packages = {}
    for label, distribution in {
        "ocrmypdf": "ocrmypdf",
        "docling": "docling",
        "pymupdf": "PyMuPDF",
        "litellm": "litellm",
        "pillow": "Pillow",
    }.items():
        try:
            packages[label] = importlib_metadata.version(distribution)
        except importlib_metadata.PackageNotFoundError:
            packages[label] = None

    commands = {}
    for label, candidates, arguments in (
        ("tesseract", ("tesseract",), ("--version",)),
        ("qpdf", ("qpdf",), ("--version",)),
        ("ghostscript", ("gswin64c", "gswin32c", "gs"), ("--version",)),
    ):
        executable = next((shutil.which(candidate) for candidate in candidates if shutil.which(candidate)), None)
        if not executable:
            commands[label] = None
            continue
        try:
            result = subprocess.run(
                [executable, *arguments],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            version_text = (result.stdout or result.stderr or "").strip().splitlines()
            commands[label] = version_text[0][:200] if version_text else "unknown"
        except Exception:
            commands[label] = "unavailable"
    return {"packages": packages, "commands": commands}


def _fingerprint(text: str) -> dict:
    text = text or ""
    return {
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "length": len(text),
    }


def build_runtime_audit(
    llm_client,
    *,
    output_format: str,
    docx_mode: str,
    large_pdf_reduced: bool,
    ocr_options: dict | None = None,
) -> dict:
    """Create non-secret, reproducible runtime metadata for a processed job."""
    prompts = getattr(llm_client, "prompts", {}) or {}
    return {
        "app_version": APP_VERSION,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "models": {
            "vision": getattr(llm_client, "vision_model", ""),
            "fusion": getattr(llm_client, "fusion_model", ""),
            "analysis": getattr(llm_client, "analysis_model", ""),
            "glm_ocr": getattr(llm_client, "glm_ocr_model", ""),
        },
        "toolchain": _toolchain_versions(),
        "ocr_options": dict(ocr_options or {}),
        "options": {
            "output_format": output_format,
            "docx_mode": docx_mode,
            "think_fusion": bool(getattr(llm_client, "think_fusion", False)),
            "think_analysis": bool(getattr(llm_client, "think_analysis", False)),
            "keep_alive": getattr(llm_client, "keep_alive", ""),
            "large_pdf_reduced": bool(large_pdf_reduced),
        },
        "prompts": {
            "version": getattr(llm_client, "prompt_version", 1),
            "fingerprints": {key: _fingerprint(value) for key, value in prompts.items()},
        },
    }
