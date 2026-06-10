"""Privacy helpers for optional redaction before external LLM calls."""

from __future__ import annotations

import re


EXTERNAL_MODEL_PREFIXES = {"openai", "gemini", "mistral", "anthropic", "cohere", "vertex_ai"}


def is_external_model(model: str | None) -> bool:
    prefix = (model or "").split("/", 1)[0].lower()
    return prefix in EXTERNAL_MODEL_PREFIXES


REDACTION_PATTERNS = [
    (re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b", re.IGNORECASE), "[IBAN]"),
    (re.compile(r"\b\d{2}\.\d{2}\.\d{4}\b"), "[DATUM]"),
    (re.compile(r"\b\d{4}-\d{2}-\d{2}\b"), "[DATUM]"),
    (re.compile(r"\b[\w.+-]+@[\w.-]+\.[a-z]{2,}\b", re.IGNORECASE), "[EMAIL]"),
    (re.compile(r"\b(?:\+49|0)[0-9][0-9\s/().-]{6,}\b"), "[TELEFON]"),
    (re.compile(r"\b(?:Versichertennummer|Kundennummer|Steuer-ID|Steuerid|Patientennummer)\s*[:#]?\s*[A-Z0-9 -]{4,}\b", re.IGNORECASE), "[ID]"),
    (re.compile(r"\b[A-ZÄÖÜ][a-zäöüß]+,\s+[A-ZÄÖÜ][a-zäöüß]+\b"), "[NAME]"),
]


def redact_sensitive_text(text: str | None) -> str:
    value = text or ""
    for pattern, replacement in REDACTION_PATTERNS:
        value = pattern.sub(replacement, value)
    return value
