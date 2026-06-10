import re
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ContextMatch:
    path: str
    score: int
    evidence: list[str] = field(default_factory=list)


OBJECT_TYPE_HINTS = {
    "vehicle": ["auto", "fahrzeug", "kfz", "service", "inspektion", "hu", "tuev", "tuv", "werkstatt"],
    "hobby": ["hobby", "verein", "mitgliedschaft", "kurs", "training"],
    "insurance": ["versicherung", "police", "schaden", "beitrag"],
    "health": ["arzt", "praxis", "befund", "rezept", "patient", "labor"],
}


def _normalize(text: Any) -> str:
    value = str(text or "").casefold()
    value = value.replace("ü", "ue").replace("ä", "ae").replace("ö", "oe").replace("ß", "ss")
    return re.sub(r"\s+", " ", value)


def _flatten_metadata(metadata: dict) -> str:
    if not isinstance(metadata, dict):
        return ""
    return " ".join(str(value) for value in metadata.values() if value is not None)


def _iter_context_terms(context: dict):
    for field_name, weight in (("aliases", 5), ("keywords", 4)):
        values = context.get(field_name, [])
        if isinstance(values, str):
            values = values.replace("\n", ",").split(",")
        if not isinstance(values, list):
            continue
        for value in values:
            term = " ".join(str(value).strip().split())
            if len(term) >= 2:
                yield field_name, term, weight


def _path_component_terms(path: str):
    for part in path.split("/"):
        part = part.strip()
        if len(part) >= 4 and part.casefold() not in {"auto", "hobby", "schule", "arbeit", "finanzen"}:
            yield part


def rank_context_paths(
    fused_text: str,
    metadata: dict,
    known_paths: list[str],
    path_contexts: dict[str, dict],
) -> list[dict]:
    """Return ranked path candidates from explicit user-provided context."""
    if not path_contexts:
        return []

    known_set = set(known_paths or [])
    haystack = _normalize(f"{_flatten_metadata(metadata)}\n{fused_text}")
    matches: list[ContextMatch] = []

    for path, context in path_contexts.items():
        if path not in known_set or not isinstance(context, dict):
            continue

        score = 0
        evidence = []
        for field_name, term, weight in _iter_context_terms(context):
            normalized_term = _normalize(term)
            if normalized_term and normalized_term in haystack:
                score += weight
                evidence.append(f"{field_name}:{term}")

        object_type = _normalize(context.get("object_type", ""))
        for hint in OBJECT_TYPE_HINTS.get(object_type, []):
            if _normalize(hint) in haystack:
                score += 1

        if evidence:
            for component in _path_component_terms(path):
                if _normalize(component) in haystack:
                    score += 2
                    evidence.append(f"path:{component}")

        if score:
            matches.append(ContextMatch(path=path, score=score, evidence=evidence))

    if not matches:
        return []

    matches.sort(key=lambda item: (item.score, item.path.count("/")), reverse=True)
    return [
        {
            "path": match.path,
            "score": min(98, match.score * 8),
            "raw_score": match.score,
            "reason": "context",
            "evidence": match.evidence[:8],
            "is_new": False,
        }
        for match in matches
    ]


def find_best_context_path(
    fused_text: str,
    metadata: dict,
    known_paths: list[str],
    path_contexts: dict[str, dict],
    *,
    min_score: int = 40,
    min_lead: int = 16,
) -> dict | None:
    """Return a high-confidence path match from explicit user-provided context."""
    matches = rank_context_paths(fused_text, metadata, known_paths, path_contexts)
    if not matches:
        return None

    best = matches[0]
    runner_up = matches[1]["score"] if len(matches) > 1 else 0
    if best["score"] >= min_score and best["score"] - runner_up >= min_lead:
        return {
            "recommended_path": best["path"],
            "is_new": False,
            "reason": "context_match",
            "score": best["score"],
            "evidence": best["evidence"],
            "candidates": matches[:5],
        }
    return None


def format_contexts_for_prompt(path_contexts: dict[str, dict], known_paths: list[str], limit: int = 80) -> str:
    """Compact, prompt-safe representation of path contexts."""
    if not path_contexts:
        return ""

    known_set = set(known_paths or [])
    lines = []
    for path in sorted(path_contexts):
        if path not in known_set:
            continue
        context = path_contexts.get(path) or {}
        aliases = ", ".join(context.get("aliases") or [])
        keywords = ", ".join(context.get("keywords") or [])
        object_type = context.get("object_type") or ""
        notes = " ".join(str(context.get("notes") or "").split())
        parts = []
        if object_type:
            parts.append(f"Typ={object_type}")
        if aliases:
            parts.append(f"Aliase={aliases}")
        if keywords:
            parts.append(f"Stichworte={keywords}")
        if notes:
            parts.append(f"Notiz={notes[:220]}")
        if parts:
            lines.append(f"- {path}: " + "; ".join(parts))
        if len(lines) >= limit:
            break
    return "\n".join(lines)
