import json
import logging
import os
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("UnifiedOCR")

MAX_DECISIONS = 1000
MAX_TERMS_PER_PATH = 120
STOPWORDS = {
    "aber", "alle", "auch", "auf", "aus", "bei", "beim", "bis", "das", "dem",
    "den", "der", "des", "die", "ein", "eine", "einer", "eines", "fuer",
    "für", "ist", "mit", "nach", "nicht", "oder", "und", "vom", "von",
    "zum", "zur", "rechnung", "datum", "seite", "betrag", "dokument",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_text(text: Any) -> str:
    value = str(text or "").casefold()
    value = value.replace("ü", "ue").replace("ä", "ae").replace("ö", "oe").replace("ß", "ss")
    return re.sub(r"\s+", " ", value)


def _split_metadata_values(metadata: dict) -> list[str]:
    if not isinstance(metadata, dict):
        return []
    values = []
    for key in ("title", "subject", "document_type", "tags"):
        raw = metadata.get(key)
        if raw:
            values.extend(re.split(r"[,;_/|\n]+", str(raw)))
    return values


def extract_learning_terms(fused_text: str, metadata: dict, limit: int = 80) -> list[str]:
    """Extract stable-ish terms for future classification matches."""
    text = f"{' '.join(_split_metadata_values(metadata))}\n{fused_text[:5000]}"
    normalized = normalize_text(text)

    candidates = []
    # Preserve IDs, customer numbers, license plates and similar mixed tokens.
    candidates.extend(re.findall(r"\b[a-z]{1,3}[-\s]?[a-z]{1,3}[-\s]?\d{2,5}\b", normalized))
    candidates.extend(re.findall(r"\b(?:kundennummer|vertragsnummer|police|kennzeichen|iban|rechnung)\s*[:#]?\s*[a-z0-9 -]{3,24}\b", normalized))
    candidates.extend(re.findall(r"\b[a-z0-9][a-z0-9-]{3,}\b", normalized))

    cleaned = []
    for value in candidates:
        term = " ".join(value.strip(" .,:;()[]{}").split())
        if len(term) < 4 or term in STOPWORDS or term.isdigit():
            continue
        cleaned.append(term)

    counts = Counter(cleaned)
    # Prefer terms from metadata and rare-looking identifiers over generic words.
    ranked = sorted(
        counts,
        key=lambda term: (
            bool(re.search(r"\d", term)),
            counts[term],
            len(term),
        ),
        reverse=True,
    )
    return ranked[:limit]


class ClassificationMemory:
    """Local learning store for confirmed folder decisions."""

    def __init__(self, base_dir: Path):
        self.base_dir = Path(base_dir)
        self.memory_file = self.base_dir / "classification_memory.json"
        self.data = self._load()

    def _defaults(self) -> dict:
        return {
            "schema": "unified_ocr_classification_memory_v1",
            "updated_at": _now_iso(),
            "decisions": [],
            "path_stats": {},
        }

    def _load(self) -> dict:
        if self.memory_file.exists():
            try:
                with open(self.memory_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                defaults = self._defaults()
                data.setdefault("schema", defaults["schema"])
                data.setdefault("decisions", [])
                data.setdefault("path_stats", {})
                return data
            except Exception as exc:
                logger.warning("Konnte classification_memory.json nicht lesen: %s", exc)
        data = self._defaults()
        self._save_data(data)
        return data

    def _save_data(self, data: dict):
        self.base_dir.mkdir(parents=True, exist_ok=True)
        data["updated_at"] = _now_iso()
        tmp_file = self.memory_file.with_suffix(".json.tmp")
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp_file, self.memory_file)

    def save(self):
        self._save_data(self.data)

    def _path_stat(self, path: str) -> dict:
        stats = self.data.setdefault("path_stats", {})
        return stats.setdefault(path, {
            "confirmed_count": 0,
            "positive_terms": {},
            "negative_terms": {},
            "document_types": {},
            "last_confirmed_at": "",
        })

    @staticmethod
    def _increment_terms(target: dict, terms: list[str], amount: int = 1):
        for term in terms:
            key = normalize_text(term)
            if not key:
                continue
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
    ):
        chosen_path = (chosen_path or "").strip().replace("\\", "/")
        if not chosen_path:
            return

        terms = extract_learning_terms(fused_text, metadata)
        document_type = normalize_text((metadata or {}).get("document_type", ""))

        stat = self._path_stat(chosen_path)
        stat["confirmed_count"] = int(stat.get("confirmed_count", 0)) + 1
        stat["last_confirmed_at"] = _now_iso()
        self._increment_terms(stat.setdefault("positive_terms", {}), terms)
        if document_type:
            self._increment_terms(stat.setdefault("document_types", {}), [document_type], amount=2)

        for candidate in candidates or []:
            candidate_path = str(candidate.get("path") or candidate.get("recommended_path") or "").strip().replace("\\", "/")
            if candidate_path and candidate_path != chosen_path:
                rejected = self._path_stat(candidate_path)
                self._increment_terms(rejected.setdefault("negative_terms", {}), terms[:30])

        if proposed_path and proposed_path != chosen_path:
            rejected = self._path_stat(proposed_path)
            self._increment_terms(rejected.setdefault("negative_terms", {}), terms[:30])

        decision = {
            "created_at": _now_iso(),
            "chosen_path": chosen_path,
            "proposed_path": proposed_path,
            "source": source,
            "document_type": (metadata or {}).get("document_type", ""),
            "terms": terms[:40],
        }
        decisions = self.data.setdefault("decisions", [])
        decisions.append(decision)
        self.data["decisions"] = decisions[-MAX_DECISIONS:]
        self._trim_path_stats()
        self.save()

    def _trim_path_stats(self):
        for stat in self.data.get("path_stats", {}).values():
            for key in ("positive_terms", "negative_terms", "document_types"):
                values = stat.get(key, {})
                if not isinstance(values, dict):
                    stat[key] = {}
                    continue
                ranked = sorted(values.items(), key=lambda item: item[1], reverse=True)
                stat[key] = dict(ranked[:MAX_TERMS_PER_PATH])

    def build_candidates(self, fused_text: str, metadata: dict, known_paths: list[str], limit: int = 5) -> list[dict]:
        known = set(known_paths or [])
        current_terms = set(extract_learning_terms(fused_text, metadata, limit=120))
        current_doc_type = normalize_text((metadata or {}).get("document_type", ""))
        candidates = []

        for path, stat in self.data.get("path_stats", {}).items():
            if path not in known or not isinstance(stat, dict):
                continue
            positive = stat.get("positive_terms", {})
            negative = stat.get("negative_terms", {})
            doc_types = stat.get("document_types", {})

            positive_hits = sorted(term for term in current_terms if term in positive)
            negative_hits = sorted(term for term in current_terms if term in negative)

            score = min(int(stat.get("confirmed_count", 0)) * 3, 18)
            score += sum(min(int(positive.get(term, 0)), 6) * 6 for term in positive_hits[:8])
            score -= sum(min(int(negative.get(term, 0)), 5) * 5 for term in negative_hits[:5])
            if current_doc_type and current_doc_type in doc_types:
                score += min(int(doc_types.get(current_doc_type, 0)), 6) * 5

            if score >= 18 and positive_hits:
                candidates.append({
                    "path": path,
                    "score": max(0, min(score, 98)),
                    "reason": "memory",
                    "evidence": positive_hits[:8],
                    "is_new": False,
                })

        candidates.sort(key=lambda item: (item["score"], item["path"].count("/")), reverse=True)
        return candidates[:limit]
