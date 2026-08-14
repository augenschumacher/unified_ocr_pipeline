"""Local learning store for explicitly confirmed folder decisions."""

from __future__ import annotations

import json
import logging
import os
import re
import unicodedata
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.cloud.context_matcher import contains_term, source_grounded_metadata
from core.metadata import build_document_excerpt


logger = logging.getLogger("UnifiedOCR")

MAX_DECISIONS = 1000
MAX_TERMS_PER_PATH = 160
MEMORY_SCHEMA = "unified_ocr_classification_memory_v2"

CONFIRMED_SOURCES = {
    "user", "manual", "manual_review", "sorting_prompt",
    "review", "confirmed", "correction", "test", "migration",
}
UNCONFIRMED_SOURCES = {
    "automatic", "auto", "llm", "context", "context_match", "memory", "pipeline",
    "fallback", "import", "prediction",
}

STOPWORDS = {
    "aber", "alle", "auch", "auf", "aus", "bei", "beim", "bis", "das", "dem",
    "den", "der", "des", "die", "ein", "eine", "einer", "eines", "fuer", "für",
    "ist", "mit", "nach", "nicht", "oder", "und", "vom", "von", "zum", "zur",
    "diese", "dieser", "dieses", "ihnen", "ihre", "sehr", "werden", "wurde",
    "rechnung", "datum", "seite", "betrag", "dokument", "anlage", "betreff",
    "information", "informationen", "mitteilung", "schreiben", "unterlagen",
}

_REFERENCE_LABELS = {
    "kundennummer": "customer",
    "kunden-nr": "customer",
    "vertragsnummer": "contract",
    "vertrags-nr": "contract",
    "policennummer": "policy",
    "police": "policy",
    "aktenzeichen": "case",
    "vorgangsnummer": "case",
    "rechnungsnummer": "invoice",
    "rechnung-nr": "invoice",
    "bestellnummer": "order",
    "mitgliedsnummer": "member",
    "kennzeichen": "vehicle",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_text(text: Any) -> str:
    value = unicodedata.normalize("NFKD", str(text or "").casefold())
    value = "".join(char for char in value if not unicodedata.combining(char))
    value = value.replace("ß", "ss")
    value = re.sub(r"\s+", " ", value)
    return value.strip(" .,:;()[]{}\"'")


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, (list, tuple, set)):
        return list(value)
    if isinstance(value, str):
        return re.split(r"[,;|\n]+", value)
    return [] if value in (None, "") else [value]


def _normalise_term(prefix: str, value: Any) -> str:
    term = normalize_text(value)
    term = re.sub(r"\s*[-/]\s*", "-", term)
    term = re.sub(r"\s+", " ", term)
    if not term:
        return ""
    return f"{prefix}:{term}"


def _metadata_terms(metadata: dict, source_text: str) -> list[str]:
    if not isinstance(metadata, dict):
        return []
    metadata = source_grounded_metadata(metadata, source_text)
    result = []

    document_type = metadata.get("document_type") or metadata.get("type")
    if document_type:
        result.append(_normalise_term("doctype", document_type))

    for tag in _as_list(metadata.get("tags") or metadata.get("tag_list") or metadata.get("keywords")):
        tag = tag.get("value") if isinstance(tag, dict) else tag
        normalised = normalize_text(tag)
        if len(normalised) >= 3 and normalised not in STOPWORDS:
            result.append(_normalise_term("tag", normalised))

    for field in ("owner", "issuer", "recipient"):
        value = metadata.get(field)
        if isinstance(value, dict):
            value = value.get("name") or value.get("value")
        normalised = normalize_text(value)
        if len(normalised) >= 3:
            result.append(_normalise_term(f"party_{field}", normalised))

    references = metadata.get("reference_ids") or metadata.get("identifiers") or []
    if isinstance(references, dict):
        references = [{"type": key, "value": value} for key, value in references.items()]
    for item in _as_list(references):
        if isinstance(item, dict):
            ref_type = normalize_text(item.get("type") or "reference")
            value = item.get("value") or item.get("id") or item.get("number")
        else:
            ref_type, value = "reference", item
        normalised = normalize_text(value)
        if len(re.sub(r"\W", "", normalised)) >= 3:
            result.append(_normalise_term(f"id_{ref_type or 'reference'}", normalised))
    return [term for term in result if term]


def _text_identifier_terms(text: str) -> list[str]:
    terms = []
    normalised = normalize_text(text)

    # International bank-account identifiers are highly discriminative.
    for match in re.findall(r"(?<!\w)[a-z]{2}\d{2}(?:[ -]?[a-z0-9]){11,30}(?!\w)", normalised):
        compact = re.sub(r"\s+", "", match)
        terms.append(_normalise_term("id_iban", compact))

    # Common German registration plate shape; exact token boundaries avoid
    # matching arbitrary prose fragments.
    for match in re.findall(r"(?<!\w)[a-zäöü]{1,3}[ -]+[a-z]{1,2}[ -]+\d{1,4}(?!\w)", normalised):
        terms.append(_normalise_term("id_vehicle", match))

    labels_pattern = "|".join(re.escape(label) for label in sorted(_REFERENCE_LABELS, key=len, reverse=True))
    pattern = re.compile(
        rf"(?<!\w)({labels_pattern})\s*[:#]?\s*([a-z0-9](?:[a-z0-9/-]{{1,}})(?:\s+[a-z0-9/-]+){{0,2}})",
        flags=re.IGNORECASE,
    )
    for label, value in pattern.findall(normalised):
        # Stop a greedy OCR phrase before common prose words.
        pieces = value.split()
        while pieces and pieces[-1] in STOPWORDS:
            pieces.pop()
        value = " ".join(pieces)
        if len(re.sub(r"\W", "", value)) >= 3:
            terms.append(_normalise_term(f"id_{_REFERENCE_LABELS[normalize_text(label)]}", value))

    # Stable product/object phrases such as "Golf 7" or "Model 3".
    for phrase in re.findall(r"(?<!\w)[a-z][a-z0-9-]{2,}\s+\d{1,5}(?!\w)", normalised):
        terms.append(_normalise_term("object", phrase))

    # Mixed alphanumeric tokens commonly used as references.
    for token in re.findall(r"(?<!\w)(?=[a-z0-9-]{5,}(?!\w))(?=[a-z0-9-]*[a-z])(?=[a-z0-9-]*\d)[a-z0-9-]+", normalised):
        terms.append(_normalise_term("id_token", token))
    return [term for term in terms if term]


def _distinctive_text_terms(text: str) -> list[str]:
    words = re.findall(r"(?<!\w)[a-zäöüß][a-zäöüß-]{7,}(?!\w)", normalize_text(text))
    counts = Counter(word.strip("-") for word in words)
    ranked = sorted(
        (word for word in counts if word not in STOPWORDS),
        key=lambda word: (counts[word], len(word), word),
        reverse=True,
    )
    return [_normalise_term("term", word) for word in ranked[:40]]


def extract_learning_terms(fused_text: str, metadata: dict, limit: int = 80) -> list[str]:
    """Extract bounded, typed terms that are stable enough to learn from."""
    excerpt = build_document_excerpt(fused_text, max_chars=12_000)
    candidates = [
        *_metadata_terms(metadata or {}, excerpt),
        *_text_identifier_terms(excerpt),
        *_distinctive_text_terms(excerpt),
    ]
    result = []
    seen = set()
    for term in candidates:
        term = normalize_text(term)
        key = term.casefold()
        if not term or key in seen:
            continue
        seen.add(key)
        result.append(term)
        if len(result) >= max(1, int(limit)):
            break
    return result


def _term_weight(term: str) -> int:
    prefix = term.split(":", 1)[0]
    if prefix.startswith("id_"):
        return 14
    if prefix.startswith("party_"):
        return 10
    if prefix == "object":
        return 9
    if prefix == "tag":
        return 7
    if prefix == "doctype":
        return 5
    if prefix == "term":
        return 3
    # Legacy v1 terms remain usable, but deliberately weak.
    return 2


def _person_routing_evidence(
    path: str,
    fused_text: str,
    grounded_metadata: dict,
    identifier_hits: list[str],
) -> tuple[bool, bool, list[str]]:
    """Return (eligible evidence, party conflict, audit evidence) for a path."""
    root = _normalise_path(path).split("/", 1)[0]
    if not root or normalize_text(root) in {"sonstiges", "unassigned", "nicht zugeordnet"}:
        return True, False, []

    parties: list[tuple[str, Any]] = []
    for field in ("owner", "recipient"):
        value = grounded_metadata.get(field) if isinstance(grounded_metadata, dict) else None
        if value:
            parties.append((field, value))
    matching_parties = [
        (field, value) for field, value in parties if contains_term(value, root)
    ]
    conflicting_parties = [
        (field, value) for field, value in parties if not contains_term(value, root)
    ]
    party_conflict = bool(conflicting_parties)

    evidence = [f"{field}:{root}" for field, _value in matching_parties]
    if contains_term(fused_text, root):
        evidence.append(f"text_person:{root}")
    if identifier_hits:
        evidence.extend(f"stable_identifier:{term}" for term in identifier_hits[:3])
    if party_conflict:
        evidence.extend(
            f"conflicting_{field}:{normalize_text(value)}"
            for field, value in conflicting_parties[:3]
        )

    has_person_or_identifier = bool(matching_parties or contains_term(fused_text, root) or identifier_hits)
    return has_person_or_identifier and not party_conflict, party_conflict, list(dict.fromkeys(evidence))


class ClassificationMemory:
    """Versioned store that learns exclusively from confirmed decisions."""

    def __init__(self, base_dir: Path):
        self.base_dir = Path(base_dir)
        self.memory_file = self.base_dir / "classification_memory.json"
        self.data = self._load()

    def _defaults(self) -> dict:
        return {
            "schema": MEMORY_SCHEMA,
            "updated_at": _now_iso(),
            "decisions": [],
            "path_stats": {},
        }

    def _load(self) -> dict:
        if self.memory_file.exists():
            try:
                with open(self.memory_file, "r", encoding="utf-8") as handle:
                    data = json.load(handle)
                if not isinstance(data, dict):
                    raise ValueError("classification memory root is not an object")
                data["schema"] = MEMORY_SCHEMA
                data["decisions"] = data.get("decisions") if isinstance(data.get("decisions"), list) else []
                data["path_stats"] = data.get("path_stats") if isinstance(data.get("path_stats"), dict) else {}
                return data
            except Exception as exc:
                logger.warning("Konnte classification_memory.json nicht lesen: %s", exc)
        data = self._defaults()
        self._save_data(data)
        return data

    def _save_data(self, data: dict):
        self.base_dir.mkdir(parents=True, exist_ok=True)
        data["schema"] = MEMORY_SCHEMA
        data["updated_at"] = _now_iso()
        tmp_file = self.memory_file.with_suffix(".json.tmp")
        with open(tmp_file, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
        os.replace(tmp_file, self.memory_file)

    def save(self):
        self._save_data(self.data)

    @staticmethod
    def _empty_stat() -> dict:
        return {
            "confirmed_count": 0,
            "positive_terms": {},
            "negative_terms": {},
            "document_types": {},
            "last_confirmed_at": "",
        }

    @staticmethod
    def _increment_terms(target: dict, terms: list[str], amount: int = 1):
        for term in terms:
            key = normalize_text(term)
            if key:
                target[key] = min(int(target.get(key, 0)) + amount, 50)

    def record_decision(
        self,
        *,
        chosen_path: str,
        fused_text: str,
        metadata: dict,
        proposed_path: str = "",
        candidates: list[dict] | None = None,
        source: str = "user",
        confirmed: bool | None = None,
        decision_id: str | None = None,
    ) -> bool:
        """Record a reviewed decision; unconfirmed predictions are ignored."""
        source_key = normalize_text(source).replace(" ", "_") or "user"
        if confirmed is False:
            logger.info("Lernspeicher ignoriert unbestätigte Entscheidung (%s).", source_key)
            return False
        if confirmed is not True and source_key in UNCONFIRMED_SOURCES:
            logger.info("Lernspeicher ignoriert unbestätigte Entscheidung (%s).", source_key)
            return False
        if confirmed is not True and source_key not in CONFIRMED_SOURCES:
            logger.warning("Lernspeicher ignoriert Quelle ohne Bestätigungssemantik: %s", source_key)
            return False

        chosen_path = _normalise_path(chosen_path)
        proposed_path = _normalise_path(proposed_path)
        if not chosen_path:
            return False

        grounded_metadata = source_grounded_metadata(metadata or {}, fused_text)
        terms = extract_learning_terms(fused_text, grounded_metadata)
        document_type = normalize_text(grounded_metadata.get("document_type", ""))
        rejected_paths = []
        for candidate in candidates or []:
            if not isinstance(candidate, dict):
                continue
            candidate_path = _normalise_path(candidate.get("path") or candidate.get("recommended_path"))
            if candidate_path and candidate_path != chosen_path and candidate_path not in rejected_paths:
                rejected_paths.append(candidate_path)
        if proposed_path and proposed_path != chosen_path and proposed_path not in rejected_paths:
            rejected_paths.append(proposed_path)

        stable_id = str(decision_id or "").strip() or uuid.uuid4().hex
        decisions = self.data.setdefault("decisions", [])
        if any(str(item.get("id") or "") == stable_id for item in decisions if isinstance(item, dict)):
            return False
        decision = {
            "id": stable_id,
            "created_at": _now_iso(),
            "chosen_path": chosen_path,
            "proposed_path": proposed_path,
            "rejected_paths": rejected_paths[:10],
            "source": source_key,
            "confirmed": True,
            "document_type": grounded_metadata.get("document_type", ""),
            "terms": terms[:80],
        }
        decisions.append(decision)
        self.data["decisions"] = decisions[-MAX_DECISIONS:]
        self._rebuild_path_stats()
        self.save()
        return True

    def forget_decision(self, decision_id: str) -> bool:
        """Remove a learned confirmation and rebuild its derived statistics."""
        decision_id = str(decision_id or "").strip()
        decisions = self.data.get("decisions", [])
        remaining = [item for item in decisions if str(item.get("id") or "") != decision_id]
        if len(remaining) == len(decisions):
            return False
        self.data["decisions"] = remaining
        self._rebuild_path_stats()
        self.save()
        return True

    def undo_last_decision(self) -> dict | None:
        decisions = self.data.get("decisions", [])
        if not decisions:
            return None
        removed = decisions.pop()
        self._rebuild_path_stats()
        self.save()
        return removed

    def _rebuild_path_stats(self):
        stats: dict[str, dict] = {}
        for decision in self.data.get("decisions", []):
            if not isinstance(decision, dict) or decision.get("confirmed") is False:
                continue
            chosen_path = _normalise_path(decision.get("chosen_path"))
            if not chosen_path:
                continue
            terms = [normalize_text(term) for term in decision.get("terms", []) if normalize_text(term)]
            stat = stats.setdefault(chosen_path, self._empty_stat())
            stat["confirmed_count"] += 1
            stat["last_confirmed_at"] = decision.get("created_at") or stat["last_confirmed_at"]
            self._increment_terms(stat["positive_terms"], terms)
            document_type = normalize_text(decision.get("document_type"))
            if document_type:
                self._increment_terms(stat["document_types"], [document_type], amount=2)

            rejected_paths = decision.get("rejected_paths") or []
            if not rejected_paths and decision.get("proposed_path") != chosen_path:
                rejected_paths = [decision.get("proposed_path")]
            for rejected_path in rejected_paths:
                rejected_path = _normalise_path(rejected_path)
                if not rejected_path or rejected_path == chosen_path:
                    continue
                rejected = stats.setdefault(rejected_path, self._empty_stat())
                self._increment_terms(rejected["negative_terms"], terms[:40])

        self.data["path_stats"] = stats
        self._trim_path_stats()

    def _trim_path_stats(self):
        for stat in self.data.get("path_stats", {}).values():
            for key in ("positive_terms", "negative_terms", "document_types"):
                values = stat.get(key, {})
                if not isinstance(values, dict):
                    stat[key] = {}
                    continue
                ranked = sorted(values.items(), key=lambda item: (int(item[1]), item[0]), reverse=True)
                stat[key] = dict(ranked[:MAX_TERMS_PER_PATH])

    def build_candidates(
        self,
        fused_text: str,
        metadata: dict,
        known_paths: list[str],
        limit: int = 5,
    ) -> list[dict]:
        known_lookup = {_normalise_path(path).casefold(): _normalise_path(path) for path in known_paths or [] if _normalise_path(path)}
        grounded_metadata = source_grounded_metadata(metadata or {}, fused_text)
        current_terms = set(extract_learning_terms(fused_text, grounded_metadata, limit=140))
        current_doc_type = normalize_text(grounded_metadata.get("document_type", ""))
        candidates = []

        for stored_path, stat in self.data.get("path_stats", {}).items():
            canonical_path = known_lookup.get(_normalise_path(stored_path).casefold())
            if not canonical_path or not isinstance(stat, dict):
                continue
            positive = stat.get("positive_terms", {}) if isinstance(stat.get("positive_terms"), dict) else {}
            negative = stat.get("negative_terms", {}) if isinstance(stat.get("negative_terms"), dict) else {}
            doc_types = stat.get("document_types", {}) if isinstance(stat.get("document_types"), dict) else {}

            positive_hits = sorted(
                (term for term in current_terms if term in positive),
                key=lambda term: (_term_weight(term), int(positive.get(term, 0)), term),
                reverse=True,
            )
            negative_hits = sorted(
                (term for term in current_terms if term in negative),
                key=lambda term: (_term_weight(term), int(negative.get(term, 0)), term),
                reverse=True,
            )
            confirmed_count = int(stat.get("confirmed_count", 0))
            base_points = min(confirmed_count * 4, 16)
            positive_points = sum(
                _term_weight(term) * min(int(positive.get(term, 0)), 4)
                for term in positive_hits[:10]
            )
            negative_points = sum(
                max(3, _term_weight(term) // 2) * min(int(negative.get(term, 0)), 4)
                for term in negative_hits[:8]
            )
            document_type_points = 0
            if current_doc_type and current_doc_type in doc_types:
                document_type_points = min(int(doc_types.get(current_doc_type, 0)) * 3, 15)
            score = base_points + positive_points + document_type_points - negative_points

            if score < 20 or not positive_hits:
                continue
            strong_hits = [term for term in positive_hits if _term_weight(term) >= 7]
            identifier_hits = [term for term in positive_hits if term.startswith("id_")]
            person_evidence, party_conflict, owner_evidence = _person_routing_evidence(
                canonical_path,
                fused_text,
                grounded_metadata,
                identifier_hits,
            )
            auto_eligible = bool(
                confirmed_count >= 2
                and person_evidence
            )
            candidates.append({
                "path": canonical_path,
                "score": max(0, min(score, 96)),
                "reason": "memory",
                "evidence": positive_hits[:10],
                "negative_evidence": negative_hits[:6],
                "is_new": False,
                "confirmed_count": confirmed_count,
                "auto_assign_eligible": auto_eligible,
                "owner_evidence": owner_evidence,
                "party_conflict": party_conflict,
                "strong_evidence_count": len(strong_hits),
                "score_breakdown": {
                    "confirmed_history": base_points,
                    "positive_terms": positive_points,
                    "document_type": document_type_points,
                    "negative_terms": -negative_points,
                },
            })

        candidates.sort(
            key=lambda item: (item["score"], item["confirmed_count"], item["path"].count("/")),
            reverse=True,
        )
        return candidates[:max(1, int(limit))]


def _normalise_path(value: Any) -> str:
    parts = [" ".join(part.strip().split()) for part in str(value or "").replace("\\", "/").split("/")]
    parts = [part for part in parts if part and part not in {".", ".."}]
    return "/".join(parts[:8])
