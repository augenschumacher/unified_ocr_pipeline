"""Evidence-based matching of user-maintained folder contexts."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Iterable


@dataclass
class ContextMatch:
    path: str
    score: int
    raw_score: int
    evidence: list[str] = field(default_factory=list)
    matched_terms: list[dict] = field(default_factory=list)
    owner_evidence: list[str] = field(default_factory=list)
    strong_evidence_count: int = 0
    score_breakdown: dict[str, int] = field(default_factory=dict)


OBJECT_TYPE_HINTS = {
    "vehicle": ["auto", "fahrzeug", "kfz", "service", "inspektion", "hu", "tuev", "tuv", "werkstatt"],
    "hobby": ["hobby", "verein", "mitgliedschaft", "kurs", "training"],
    "insurance": ["versicherung", "police", "schaden", "beitrag"],
    "health": ["arzt", "praxis", "befund", "rezept", "patient", "labor"],
    "finance": ["bank", "konto", "rechnung", "steuer", "finanzen"],
    "education": ["schule", "universitaet", "studium", "zeugnis", "kurs"],
}

_GENERIC_PATH_TERMS = {
    "auto", "hobby", "schule", "arbeit", "finanzen", "gesundheit", "dokumente",
    "rechnung", "rechnungen", "sonstiges", "archiv", "privat", "allgemein",
}

# Only these semantic metadata fields can be useful for deterministic folder
# routing.  Technical/derived fields (schema version, confidence, filename
# title, unknown-field lists, and similar values) must never become routing
# terms merely because an LLM emitted them.
_ROUTING_METADATA_FIELDS = (
    "document_date",
    "title",
    "document_type",
    "tags",
    "issuer",
    "recipient",
    "owner",
    "reference_ids",
    "period",
    "amount",
    "currency",
)


def _normalize(text: Any) -> str:
    value = unicodedata.normalize("NFKD", str(text or "").casefold())
    value = "".join(char for char in value if not unicodedata.combining(char))
    value = value.replace("ß", "ss")
    return re.sub(r"\s+", " ", value).strip()


def _term_pattern(term: Any) -> re.Pattern | None:
    """Build an exact token/phrase pattern; never use raw substring matching."""
    tokens = re.findall(r"\w+", _normalize(term), flags=re.UNICODE)
    if not tokens:
        return None
    # Separators vary heavily in OCR ("AB CD 123", "AB-CD-123", "AB/CD/123").
    body = r"[\W_]+".join(re.escape(token) for token in tokens)
    return re.compile(rf"(?<!\w){body}(?!\w)", flags=re.UNICODE)


def contains_term(haystack: Any, term: Any) -> bool:
    """Return true only for a complete word or complete multi-token phrase."""
    pattern = _term_pattern(term)
    return bool(pattern and pattern.search(_normalize(haystack)))


def source_grounded_metadata(metadata: dict, fused_text: str) -> dict:
    """Return a routing-only view containing values evidenced in source text.

    Analysis metadata is intentionally kept unchanged elsewhere for display and
    review.  This projection prevents an invented owner, tag, or identifier
    from becoming deterministic folder evidence.  Every retained scalar must
    independently occur as a complete token or phrase in the OCR/source text.
    """
    if not isinstance(metadata, dict) or not str(fused_text or "").strip():
        return {}

    def grounded(value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, dict):
            # Reference objects keep their type only when the actual identifier
            # value is grounded.  For other nested structures, retain only
            # independently grounded leaves.
            if "value" in value:
                actual_value = value.get("value")
                if actual_value and contains_term(fused_text, actual_value):
                    return dict(value)
                return None
            result = {}
            for key, nested in value.items():
                retained = grounded(nested)
                if retained not in (None, "", [], {}):
                    result[key] = retained
            return result or None
        if isinstance(value, (list, tuple, set)):
            retained_values = []
            for nested in value:
                retained = grounded(nested)
                if retained not in (None, "", [], {}) and retained not in retained_values:
                    retained_values.append(retained)
            return retained_values or None
        text = str(value).strip()
        return value if text and contains_term(fused_text, text) else None

    result = {}
    for field_name in _ROUTING_METADATA_FIELDS:
        retained = grounded(metadata.get(field_name))
        if retained not in (None, "", [], {}):
            result[field_name] = retained
    return result


def _metadata_values(value: Any) -> Iterable[str]:
    if value is None:
        return
    if isinstance(value, dict):
        for key, nested in value.items():
            if key in {"field_confidence", "evidence", "unknown_fields", "schema_version"}:
                continue
            yield from _metadata_values(nested)
        return
    if isinstance(value, (list, tuple, set)):
        for nested in value:
            yield from _metadata_values(nested)
        return
    text = str(value).strip()
    if text:
        yield text


def _flatten_metadata(metadata: dict) -> str:
    return " ".join(_metadata_values(metadata)) if isinstance(metadata, dict) else ""


def _as_values(value: Any) -> list[str]:
    if isinstance(value, str):
        value = re.split(r"[,;|\n]+", value)
    if not isinstance(value, (list, tuple, set)):
        return []
    result = []
    for item in value:
        text = " ".join(str(item or "").strip().split())
        if text and text not in result:
            result.append(text)
    return result


def _iter_context_terms(context: dict):
    for field_name, weight in (("aliases", 7), ("keywords", 6)):
        for term in _as_values(context.get(field_name, [])):
            tokens = re.findall(r"\w+", _normalize(term), flags=re.UNICODE)
            # One-character prose tokens are too collision-prone. Identifiers with
            # digits remain useful even when one component is short.
            if not tokens or (len("".join(tokens)) < 2 and not any(char.isdigit() for char in term)):
                continue
            yield field_name, term, weight


def _path_component_terms(path: str):
    for part in str(path or "").split("/"):
        part = part.strip()
        normalised = _normalize(part)
        if len(normalised) >= 4 and normalised not in _GENERIC_PATH_TERMS:
            yield part


def _is_strong_term(term: str) -> bool:
    tokens = re.findall(r"\w+", _normalize(term), flags=re.UNICODE)
    return bool(
        re.search(r"\d", term)
        or len(tokens) >= 2
        or (tokens and len(tokens[0]) >= 7)
    )


def _is_stable_identifier_term(term: str) -> bool:
    """Return true for reference-like aliases, not ordinary names or prose."""
    normalised = _normalize(term)
    compact = re.sub(r"\W+", "", normalised, flags=re.UNICODE)
    digits = re.findall(r"\d", compact)
    if not digits:
        return False
    # A plain year/serial can be stable; mixed identifiers need enough digits
    # to exclude generic product names such as "Golf 7" or "Model 3".
    if compact.isdigit():
        return len(compact) >= 4
    return len(compact) >= 5 and len(digits) >= 2


def _party_conflicts_with_root(root: str, metadata: dict) -> bool:
    grounded_parties = []
    if isinstance(metadata, dict):
        for field_name in ("owner", "recipient"):
            value = metadata.get(field_name)
            if value:
                grounded_parties.append(value)
    return any(not contains_term(value, root) for value in grounded_parties)


def _owner_evidence(path: str, fused_text: str, metadata: dict) -> list[str]:
    root = next((part.strip() for part in str(path or "").split("/") if part.strip()), "")
    if not root or _normalize(root) in {"sonstiges", "unassigned", "nicht zugeordnet"}:
        return []
    if _party_conflicts_with_root(root, metadata):
        return []

    evidence = []
    if isinstance(metadata, dict):
        for field_name in ("owner", "recipient"):
            value = metadata.get(field_name)
            if value and contains_term(value, root):
                evidence.append(f"{field_name}:{root}")
        # An issuer is not automatically the owner. It is intentionally excluded.
    if contains_term(fused_text, root):
        evidence.append(f"text_person:{root}")
    return list(dict.fromkeys(evidence))


def rank_context_paths(
    fused_text: str,
    metadata: dict,
    known_paths: list[str],
    path_contexts: dict[str, dict],
) -> list[dict]:
    """Rank known paths using bounded phrases and explicit evidence."""
    if not path_contexts:
        return []

    known_lookup = {_normalize(path): path for path in known_paths or []}
    routing_metadata = source_grounded_metadata(metadata, fused_text)
    haystack = f"{_flatten_metadata(routing_metadata)}\n{fused_text or ''}"
    matches: list[ContextMatch] = []

    for configured_path, context in path_contexts.items():
        canonical_path = known_lookup.get(_normalize(configured_path))
        if not canonical_path or not isinstance(context, dict):
            continue

        term_points = identifier_bonus = object_points = path_points = owner_points = 0
        evidence: list[str] = []
        matched_terms: list[dict] = []
        strong_count = 0

        for field_name, term, weight in _iter_context_terms(context):
            if not contains_term(haystack, term):
                continue
            strong = _is_strong_term(term)
            bonus = 0
            if re.search(r"\d", term):
                bonus += 4
            if len(re.findall(r"\w+", _normalize(term), flags=re.UNICODE)) >= 2:
                bonus += 2
            term_points += weight
            identifier_bonus += bonus
            strong_count += int(strong)
            evidence.append(f"{field_name}:{term}")
            matched_terms.append({"field": field_name, "term": term, "strong": strong, "points": weight + bonus})

        owner_hits = _owner_evidence(canonical_path, fused_text, routing_metadata)
        # A context binds an object to the path owner only when the user opted
        # into that semantic or a stable identifier (contract number, plate,
        # account reference, ...) matched.  A plain alias/name is merely a
        # ranking hint and must never manufacture person evidence.
        binding_terms = [
            item for item in matched_terms
            if _is_stable_identifier_term(str(item.get("term") or ""))
            or (bool(context.get("binds_owner", False)) and item.get("field") == "aliases")
        ]
        root = canonical_path.split("/", 1)[0]
        if (
            not owner_hits
            and binding_terms
            and not _party_conflicts_with_root(root, routing_metadata)
        ):
            owner_hits = [f"context_owner_binding:{root}"]
        if owner_hits:
            owner_points = 10
            evidence.extend(owner_hits)

        object_type = _normalize(context.get("object_type", ""))
        for hint in OBJECT_TYPE_HINTS.get(object_type, []):
            if contains_term(haystack, hint):
                object_points = min(object_points + 1, 3)

        if matched_terms:
            for component in _path_component_terms(canonical_path):
                if contains_term(haystack, component):
                    path_points += 2
                    evidence.append(f"path:{component}")

        raw_score = term_points + identifier_bonus + object_points + path_points + owner_points
        if not matched_terms:
            continue
        calibrated = min(98, raw_score * 4)
        score_breakdown = {
            "context_terms": term_points,
            "identifier_bonus": identifier_bonus,
            "owner": owner_points,
            "object_type": object_points,
            "path_components": path_points,
        }
        matches.append(ContextMatch(
            path=canonical_path,
            score=calibrated,
            raw_score=raw_score,
            evidence=list(dict.fromkeys(evidence))[:12],
            matched_terms=matched_terms[:10],
            owner_evidence=owner_hits,
            strong_evidence_count=strong_count,
            score_breakdown=score_breakdown,
        ))

    matches.sort(key=lambda item: (item.score, item.strong_evidence_count, item.path.count("/")), reverse=True)
    return [
        {
            "path": match.path,
            "score": match.score,
            "raw_score": match.raw_score,
            "reason": "context",
            "evidence": match.evidence,
            "matched_terms": match.matched_terms,
            "owner_evidence": match.owner_evidence,
            "strong_evidence_count": match.strong_evidence_count,
            "score_breakdown": match.score_breakdown,
            "is_new": False,
            "auto_assign_eligible": bool(
                match.owner_evidence and match.strong_evidence_count >= 1
            ),
        }
        for match in matches
    ]


def find_best_context_path(
    fused_text: str,
    metadata: dict,
    known_paths: list[str],
    path_contexts: dict[str, dict],
    *,
    min_score: int = 72,
    min_lead: int = 18,
) -> dict | None:
    """Return only a sufficiently evidenced and unambiguous context match."""
    matches = rank_context_paths(fused_text, metadata, known_paths, path_contexts)
    if not matches:
        return None

    best = matches[0]
    runner_up = matches[1]["score"] if len(matches) > 1 else 0
    if (
        best["score"] >= min_score
        and best["score"] - runner_up >= min_lead
        and best.get("auto_assign_eligible")
    ):
        return {
            "recommended_path": best["path"],
            "is_new": False,
            "reason": "context_match",
            "decision": "auto_assign",
            "auto_assign": True,
            "review_required": False,
            "score": best["score"],
            "confidence": best["score"],
            "evidence": best["evidence"],
            "owner_evidence": best.get("owner_evidence", []),
            "score_breakdown": best.get("score_breakdown", {}),
            "candidates": matches[:5],
        }
    return None


def format_contexts_for_prompt(path_contexts: dict[str, dict], known_paths: list[str], limit: int = 80) -> str:
    """Return a compact prompt representation; it is context, never an instruction."""
    if not path_contexts:
        return ""

    known_lookup = {_normalize(path): path for path in known_paths or []}
    lines = []
    for configured_path in sorted(path_contexts):
        path = known_lookup.get(_normalize(configured_path))
        if not path:
            continue
        context = path_contexts.get(configured_path) or {}
        if not isinstance(context, dict):
            continue
        aliases = ", ".join(_as_values(context.get("aliases")))
        keywords = ", ".join(_as_values(context.get("keywords")))
        object_type = " ".join(str(context.get("object_type") or "").split())
        notes = " ".join(str(context.get("notes") or "").split())
        parts = []
        if object_type:
            parts.append(f"Typ={object_type[:80]}")
        if aliases:
            parts.append(f"Aliase={aliases[:240]}")
        if keywords:
            parts.append(f"Stichworte={keywords[:320]}")
        if notes:
            parts.append(f"Notiz={notes[:220]}")
        if parts:
            lines.append(f"- {path}: " + "; ".join(parts))
        if len(lines) >= limit:
            break
    return "\n".join(lines)
