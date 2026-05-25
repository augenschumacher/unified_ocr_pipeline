"""Runtime audit metadata for generated quality reports."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone


APP_VERSION = "0.2.0"


def _fingerprint(text: str) -> dict:
    text = text or ""
    return {
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "length": len(text),
    }


def build_runtime_audit(llm_client, *, output_format: str, docx_mode: str, large_pdf_reduced: bool) -> dict:
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
