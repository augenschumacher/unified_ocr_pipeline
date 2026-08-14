"""Archival metadata validation and compatibility helpers.

The LLM is an untrusted extractor.  This module turns its often inconsistent
output into a small, predictable schema without inventing missing values.
Flat keys used by the existing pipeline are retained, while richer archival
fields are added alongside them.
"""

from __future__ import annotations

import ast
import json
import re
import unicodedata
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping


SCHEMA_VERSION = "unified_ocr_archival_metadata_v2"

_UNKNOWN = {
    "", "-", "?", "n/a", "na", "none", "null", "unknown", "unbekannt",
    "nicht bekannt", "nicht angegeben", "ohne angabe", "heute", "today",
}

_FIELD_ALIASES = {
    "document_date": ("document_date", "date", "datum", "dokumentdatum", "documentDate"),
    "title": ("title", "titel", "document_title", "kurztitel"),
    "document_type": ("document_type", "documentType", "type", "typ", "dokumenttyp", "category"),
    "tags": ("tags", "tag_list", "keywords", "keyword", "stichworte", "schlagworte"),
    "issuer": ("issuer", "sender", "absender", "creator", "ersteller", "aussteller"),
    "recipient": ("recipient", "receiver", "empfaenger", "empfanger", "adressat"),
    "owner": ("owner", "eigentuemer", "eigentumer", "akteninhaber", "person"),
    "language": ("language", "lang", "sprache"),
    "reference_ids": (
        "reference_ids", "reference_id", "references", "identifiers", "ids",
        "referenzen", "aktenzeichen", "vorgangsnummer", "vertragsnummer",
    ),
    "period": ("period", "zeitraum", "coverage", "date_range"),
    "amount": ("amount", "total_amount", "sum", "betrag", "gesamtbetrag"),
    "currency": ("currency", "waehrung", "wahrung"),
}

_CANONICAL_EVIDENCE_FIELDS = {
    "document_date", "title", "document_type", "tags", "issuer", "recipient",
    "owner", "language", "reference_ids", "period", "amount", "currency",
}

_LANGUAGE_ALIASES = {
    "de": "de", "deu": "de", "ger": "de", "deutsch": "de", "german": "de",
    "en": "en", "eng": "en", "englisch": "en", "english": "en",
    "fr": "fr", "fra": "fr", "fre": "fr", "franzoesisch": "fr", "french": "fr",
    "it": "it", "ita": "it", "italienisch": "it", "italian": "it",
    "es": "es", "spa": "es", "spanisch": "es", "spanish": "es",
    "nl": "nl", "nld": "nl", "dut": "nl", "niederlaendisch": "nl", "dutch": "nl",
    "und": "und", "unbekannt": "und", "unknown": "und",
}

_CURRENCY_ALIASES = {
    "EUR": "EUR", "EURO": "EUR", "EUROS": "EUR", "\u20ac": "EUR",
    "USD": "USD", "US$": "USD", "$": "USD", "DOLLAR": "USD",
    "GBP": "GBP", "\u00a3": "GBP", "CHF": "CHF", "SFR": "CHF",
}

_TAG_ALIASES = {
    "rechnung": "Rechnung",
    "rechnungen": "Rechnung",
    "invoice": "Rechnung",
    "faktura": "Rechnung",
    "quittung": "Quittung",
    "receipt": "Quittung",
    "vertrag": "Vertrag",
    "vertrage": "Vertrag",
    "contract": "Vertrag",
    "lohnabrechnung": "Lohnabrechnung",
    "gehaltsabrechnung": "Lohnabrechnung",
    "payroll": "Lohnabrechnung",
    "versicherung": "Versicherung",
    "insurance": "Versicherung",
    "steuer": "Steuer",
    "steuern": "Steuer",
    "tax": "Steuer",
    "banking": "Bank",
    "gesundheit": "Gesundheit",
    "medical": "Gesundheit",
    "medizin": "Gesundheit",
    "auto": "Fahrzeug",
    "kfz": "Fahrzeug",
    "vehicle": "Fahrzeug",
    "miete": "Wohnen",
    "rental": "Wohnen",
    "beschaftigung": "Arbeit",
    "employment": "Arbeit",
    "bildung": "Bildung",
    "education": "Bildung",
    "behorde": "Behörde",
    "authority": "Behörde",
    "correspondence": "Korrespondenz",
    "warranty": "Garantie",
    "order": "Bestellung",
}

_GENERIC_TAGS = {
    "datei", "dokument", "dokumente", "information", "informationen",
    "ocr", "scan", "text", "unterlage", "unterlagen", "unbekannt",
    "unknown", "misc", "sonstiges", "allgemein",
}


def _ascii_key(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(char for char in text if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", "_", text.casefold()).strip("_")


def _clean_scalar(value: Any, *, limit: int = 500) -> str:
    if isinstance(value, Mapping):
        for key in ("value", "text", "name", "label", "title"):
            if key in value:
                value = value.get(key)
                break
        else:
            return ""
    if isinstance(value, (list, tuple, set)):
        value = next((item for item in value if item not in (None, "")), "")
    text = unicodedata.normalize("NFC", str(value or ""))
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", text)
    text = " ".join(text.strip().strip("`\"'").split())
    if text.casefold() in _UNKNOWN:
        return ""
    return text[:limit].strip()


def _containers(raw: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    result = [raw]
    for key in ("metadata", "document", "archival_metadata", "result", "analysis"):
        nested = raw.get(key)
        if isinstance(nested, Mapping):
            result.append(nested)
    return result


def _lookup(raw: Mapping[str, Any], field: str) -> Any:
    aliases = {_ascii_key(alias) for alias in _FIELD_ALIASES[field]}
    for container in _containers(raw):
        for key, value in container.items():
            if _ascii_key(key) in aliases:
                return value
    return None


def _parse_date(value: Any) -> tuple[str, str]:
    """Return (ISO value, precision); unknown/invalid values return ("", "unknown")."""
    text = _clean_scalar(value, limit=80)
    if not text:
        return "", "unknown"
    text = re.sub(r"(?:T|\s)\d{1,2}:\d{2}(?::\d{2})?.*$", "", text).strip()

    if re.fullmatch(r"(?:19|20)\d{2}", text):
        return text, "year"
    match = re.fullmatch(r"((?:19|20)\d{2})[-/.](0?[1-9]|1[0-2])", text)
    if match:
        return f"{int(match.group(1)):04d}-{int(match.group(2)):02d}", "month"

    formats = (
        "%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d",
        "%d-%m-%Y", "%d.%m.%Y", "%d/%m/%Y",
        "%d-%m-%y", "%d.%m.%y", "%d/%m/%y",
    )
    for fmt in formats:
        try:
            parsed = datetime.strptime(text, fmt)
            return parsed.strftime("%Y-%m-%d"), "day"
        except ValueError:
            continue
    return "", "unknown"


def _legacy_date(iso_value: str) -> str:
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", iso_value or ""):
        year, month, day = iso_value.split("-")
        return f"{day}-{month}-{year}"
    return iso_value or ""


def _tag_values(value: Any) -> list[Any]:
    if isinstance(value, Mapping):
        value = value.get("values") or value.get("items") or value.get("tags") or value.get("value")
    if isinstance(value, str):
        return re.split(r"[,;|\n]+", value)
    elif isinstance(value, (list, tuple, set)):
        return list(value)
    elif value is None:
        return []
    return [value]


def _raw_tags(value: Any, *, limit: int = 30) -> list[str]:
    result = []
    seen = set()
    for item in _tag_values(value):
        if isinstance(item, Mapping):
            item = item.get("value") or item.get("name") or item.get("label")
        tag = _clean_scalar(item, limit=80).strip("#.,;: ")
        key = tag.casefold()
        if tag and key not in seen:
            result.append(tag)
            seen.add(key)
        if len(result) >= limit:
            break
    return result


def normalize_tags(value: Any, *, limit: int = 12) -> list[str]:
    """Return concise, de-duplicated tags with conservative canonical aliases.

    Domain-specific terms outside the small alias vocabulary are retained;
    generic LLM filler and sentence-like tags are removed.  Reference numbers
    belong in ``reference_ids`` and therefore are not promoted as tags.
    """
    values = _raw_tags(value)

    tags: list[str] = []
    seen: set[str] = set()
    for item in values:
        tag = _clean_scalar(item, limit=80).strip("#.,;: ")
        ascii_key = _ascii_key(tag)
        if (
            not tag
            or len(tag) < 2
            or len(tag) > 50
            or ascii_key in _GENERIC_TAGS
            or len(tag.split()) > 5
            or re.fullmatch(r"[\W\d_]+", tag)
        ):
            continue
        tag = _TAG_ALIASES.get(ascii_key, tag)
        key = _ascii_key(tag)
        if key in seen:
            continue
        seen.add(key)
        tags.append(tag)
        if len(tags) >= limit:
            break
    return tags


def _tag_keys(tags: list[str]) -> list[str]:
    return [_ascii_key(tag).replace("_", "-") for tag in tags if _ascii_key(tag)]


def _normalise_language(value: Any) -> str:
    text = _ascii_key(_clean_scalar(value, limit=80))
    if not text:
        return "und"
    if text in _LANGUAGE_ALIASES:
        return _LANGUAGE_ALIASES[text]
    first = re.split(r"[_+,;/ ]+", text)[0]
    if first in _LANGUAGE_ALIASES:
        return _LANGUAGE_ALIASES[first]
    if re.fullmatch(r"[a-z]{2,3}", first):
        return first[:2]
    return "und"


def _normalise_currency(value: Any) -> str:
    text = _clean_scalar(value, limit=30).upper().replace(" ", "")
    return _CURRENCY_ALIASES.get(text, text if re.fullmatch(r"[A-Z]{3}", text) else "")


def _normalise_amount(value: Any) -> tuple[str, str]:
    currency = ""
    if isinstance(value, Mapping):
        currency = _normalise_currency(value.get("currency") or value.get("waehrung"))
        value = value.get("value") or value.get("amount") or value.get("betrag")
    text = _clean_scalar(value, limit=80)
    if not text:
        return "", currency

    for token, code in _CURRENCY_ALIASES.items():
        if token and token.casefold() in text.casefold():
            currency = currency or code
            break
    match = re.search(r"[-+]?\d[\d .,'\u00a0]*", text)
    if not match:
        return "", currency
    number = match.group(0).replace("\u00a0", "").replace(" ", "").replace("'", "")
    negative = number.startswith("-")
    number = number.lstrip("+-")
    if "," in number and "." in number:
        decimal_separator = "," if number.rfind(",") > number.rfind(".") else "."
        thousands_separator = "." if decimal_separator == "," else ","
        number = number.replace(thousands_separator, "").replace(decimal_separator, ".")
    elif "," in number:
        tail = number.rsplit(",", 1)[1]
        number = number.replace(",", ".") if len(tail) in (1, 2) else number.replace(",", "")
    elif number.count(".") > 1:
        number = number.replace(".", "")
    elif "." in number and len(number.rsplit(".", 1)[1]) == 3:
        number = number.replace(".", "")
    if negative:
        number = "-" + number
    try:
        decimal = Decimal(number)
    except InvalidOperation:
        return "", currency
    if not decimal.is_finite() or abs(decimal) >= Decimal("1000000000000000"):
        return "", currency
    normalised = format(decimal, "f")
    if "." in normalised:
        normalised = normalised.rstrip("0").rstrip(".")
    return normalised, currency


def _normalise_reference_ids(value: Any, *, limit: int = 30) -> list[dict[str, str]]:
    items: list[Any]
    if isinstance(value, Mapping):
        if any(key in value for key in ("value", "id", "number", "nummer")):
            items = [value]
        else:
            items = [{"type": key, "value": item} for key, item in value.items()]
    elif isinstance(value, str):
        items = re.split(r"[;|\n]+", value)
    elif isinstance(value, (list, tuple, set)):
        items = list(value)
    elif value is None:
        items = []
    else:
        items = [value]

    result: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in items:
        ref_type = "reference"
        if isinstance(item, Mapping):
            ref_type = _clean_scalar(item.get("type") or item.get("kind") or item.get("label"), limit=50) or ref_type
            item = item.get("value") or item.get("id") or item.get("number") or item.get("nummer")
        text = _clean_scalar(item, limit=120)
        if not text:
            continue
        if ":" in text and ref_type == "reference":
            maybe_type, maybe_value = text.split(":", 1)
            if 1 < len(maybe_type.strip()) <= 40 and maybe_value.strip():
                ref_type, text = maybe_type.strip(), maybe_value.strip()
        key = (_ascii_key(ref_type) or "reference", re.sub(r"\s+", " ", text).casefold())
        if key in seen:
            continue
        seen.add(key)
        result.append({"type": key[0], "value": text})
        if len(result) >= limit:
            break
    return result


def _normalise_period(value: Any, raw: Mapping[str, Any]) -> dict[str, Any]:
    start_value = end_value = label = None
    if isinstance(value, Mapping):
        start_value = value.get("start") or value.get("from") or value.get("von")
        end_value = value.get("end") or value.get("to") or value.get("bis")
        label = value.get("label") or value.get("text")
    elif isinstance(value, (list, tuple)) and value:
        start_value = value[0]
        end_value = value[1] if len(value) > 1 else None
    elif value:
        label = value
        date_parts = re.findall(r"(?:19|20)\d{2}(?:[-/.]\d{1,2}(?:[-/.]\d{1,2})?)?|\d{1,2}[-/.]\d{1,2}[-/.](?:19|20)?\d{2}", str(value))
        if date_parts:
            start_value = date_parts[0]
            end_value = date_parts[1] if len(date_parts) > 1 else date_parts[0]

    for container in _containers(raw):
        start_value = start_value or container.get("period_start") or container.get("start_date") or container.get("zeitraum_von")
        end_value = end_value or container.get("period_end") or container.get("end_date") or container.get("zeitraum_bis")

    start, start_precision = _parse_date(start_value)
    end, end_precision = _parse_date(end_value)
    if start and end and len(start) == len(end) == 10 and start > end:
        start, end = end, start
        start_precision, end_precision = end_precision, start_precision
    return {
        "start": start or None,
        "end": end or None,
        "start_precision": start_precision,
        "end_precision": end_precision,
        "label": _clean_scalar(label, limit=160) or None,
    }


def _normalise_confidence(raw: Mapping[str, Any]) -> dict[str, float]:
    source: Mapping[str, Any] = {}
    for container in _containers(raw):
        candidate = container.get("field_confidence") or container.get("field_confidences")
        if isinstance(candidate, Mapping):
            source = candidate
            break
        candidate = container.get("confidence")
        if isinstance(candidate, Mapping):
            source = candidate
            break

    result: dict[str, float] = {}
    alias_to_field = {
        _ascii_key(alias): field
        for field, aliases in _FIELD_ALIASES.items()
        for alias in aliases
    }
    for key, value in source.items():
        field = alias_to_field.get(_ascii_key(key), _ascii_key(key))
        if field not in _CANONICAL_EVIDENCE_FIELDS:
            continue
        try:
            confidence = float(value)
        except (TypeError, ValueError):
            continue
        if confidence > 1 and confidence <= 100:
            confidence /= 100
        if 0 <= confidence <= 1:
            result[field] = round(confidence, 4)

    for field in _CANONICAL_EVIDENCE_FIELDS:
        nested = _lookup(raw, field)
        if isinstance(nested, Mapping) and field not in result:
            try:
                confidence = float(nested.get("confidence"))
                if confidence > 1 and confidence <= 100:
                    confidence /= 100
                if 0 <= confidence <= 1:
                    result[field] = round(confidence, 4)
            except (TypeError, ValueError):
                pass
    return result


def _normalised_phrase(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or "").casefold())
    text = "".join(char for char in text if not unicodedata.combining(char))
    return " ".join(text.split())


def _evidence_item(
    value: Any,
    source_text: str,
    source_pages: Mapping[int, str] | None = None,
) -> dict[str, Any] | None:
    if isinstance(value, str):
        value = {"quote": value}
    if not isinstance(value, Mapping):
        return None
    quote = _clean_scalar(
        value.get("quote") or value.get("text") or value.get("excerpt") or value.get("value"),
        limit=500,
    )
    if not quote:
        return None
    result: dict[str, Any] = {"quote": quote}
    page = 0
    try:
        page = int(value.get("page") or value.get("page_number") or 0)
        if page > 0:
            result["page"] = page
    except (TypeError, ValueError):
        pass
    source = _ascii_key(value.get("source") or "llm")
    result["source"] = source or "llm"
    if source_text:
        normalised_source = _normalised_phrase(source_text)
        normalised_quote = _normalised_phrase(quote)
        result["verified_in_text"] = normalised_quote in normalised_source
    if page > 0:
        if source_pages is None:
            result["page_verified"] = False
            result["page_error"] = "page_verification_unavailable"
        else:
            page_lookup = {
                int(page_number): str(page_text or "")
                for page_number, page_text in source_pages.items()
                if str(page_number).isdigit()
            }
            page_text = page_lookup.get(page)
            result["page_verified"] = bool(
                page_text
                and _normalised_phrase(quote) in _normalised_phrase(page_text)
            )
            if page_text is None:
                result["page_error"] = "page_not_available"
            elif not result["page_verified"]:
                result["page_error"] = "quote_not_on_claimed_page"
    return result


def _normalise_evidence(
    raw: Mapping[str, Any],
    source_text: str,
    source_pages: Mapping[int, str] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    evidence_raw: Any = None
    for container in _containers(raw):
        if "evidence" in container:
            evidence_raw = container.get("evidence")
            break
        if "field_evidence" in container:
            evidence_raw = container.get("field_evidence")
            break

    grouped: dict[str, list[Any]] = {}
    if isinstance(evidence_raw, Mapping):
        grouped = {
            _ascii_key(key): value if isinstance(value, list) else [value]
            for key, value in evidence_raw.items()
        }
    elif isinstance(evidence_raw, list):
        for item in evidence_raw:
            if not isinstance(item, Mapping):
                continue
            field = _ascii_key(item.get("field") or item.get("key"))
            grouped.setdefault(field, []).append(item)

    alias_to_field = {
        _ascii_key(alias): field
        for field, aliases in _FIELD_ALIASES.items()
        for alias in aliases
    }
    result: dict[str, list[dict[str, Any]]] = {}
    for key, values in grouped.items():
        field = alias_to_field.get(key, key)
        if field not in _CANONICAL_EVIDENCE_FIELDS:
            continue
        items = []
        for value in values[:10]:
            item = _evidence_item(value, source_text, source_pages)
            if item and item not in items:
                items.append(item)
        if items:
            result[field] = items

    for field in _CANONICAL_EVIDENCE_FIELDS:
        nested = _lookup(raw, field)
        if isinstance(nested, Mapping) and nested.get("evidence") and field not in result:
            values = nested["evidence"] if isinstance(nested["evidence"], list) else [nested["evidence"]]
            items = [
                item
                for item in (
                    _evidence_item(value, source_text, source_pages)
                    for value in values[:10]
                )
                if item
            ]
            if items:
                result[field] = items
    return result


def empty_metadata() -> dict[str, Any]:
    """Return a complete metadata object with explicit unknown values."""
    period = {
        "start": None, "end": None,
        "start_precision": "unknown", "end_precision": "unknown", "label": None,
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "date": "",
        "document_date": None,
        "date_precision": "unknown",
        "date_status": "unknown",
        "title": "",
        "filename_title": "",
        "subject": "",
        "document_type": "",
        "tags": [],
        "raw_tags": [],
        "tag_keys": [],
        "tags_text": "",
        "issuer": "",
        "recipient": "",
        "owner": "",
        "language": "und",
        "reference_ids": [],
        "period": period,
        "amount": "",
        "currency": "",
        "field_confidence": {},
        "evidence": {},
        "unknown_fields": [
            "document_date", "title", "document_type", "tags", "issuer", "recipient",
            "owner", "language", "reference_ids", "period", "amount", "currency",
        ],
    }


def normalize_metadata(
    raw: Any,
    *,
    source_text: str = "",
    source_pages: Mapping[int, str] | None = None,
) -> dict[str, Any]:
    """Validate model output and return the canonical dict-compatible schema.

    Missing dates stay empty/``None``.  In particular, this function never
    substitutes the current date.
    """
    if not isinstance(raw, Mapping):
        return empty_metadata()

    result = empty_metadata()
    document_date, precision = _parse_date(_lookup(raw, "document_date"))
    result["document_date"] = document_date or None
    result["date"] = _legacy_date(document_date)
    result["date_precision"] = precision
    result["date_status"] = "known" if document_date else "unknown"

    title = _clean_scalar(_lookup(raw, "title"), limit=240)
    document_type = _clean_scalar(_lookup(raw, "document_type"), limit=120)
    raw_tag_value = _lookup(raw, "tags")
    raw_tags = _raw_tags(raw_tag_value)
    tags = normalize_tags(raw_tag_value)
    result["title"] = title
    result["filename_title"] = re.sub(r"_+", "_", re.sub(r"[^\w.-]+", "_", title, flags=re.UNICODE)).strip("_.")[:120]
    result["document_type"] = document_type
    result["tags"] = tags
    result["raw_tags"] = raw_tags
    result["tag_keys"] = _tag_keys(tags)
    result["tags_text"] = ", ".join(tags)
    result["issuer"] = _clean_scalar(_lookup(raw, "issuer"), limit=240)
    result["recipient"] = _clean_scalar(_lookup(raw, "recipient"), limit=240)
    result["owner"] = _clean_scalar(_lookup(raw, "owner"), limit=240)
    result["language"] = _normalise_language(_lookup(raw, "language"))
    result["reference_ids"] = _normalise_reference_ids(_lookup(raw, "reference_ids"))
    result["period"] = _normalise_period(_lookup(raw, "period"), raw)

    amount, embedded_currency = _normalise_amount(_lookup(raw, "amount"))
    result["amount"] = amount
    result["currency"] = _normalise_currency(_lookup(raw, "currency")) or embedded_currency
    result["field_confidence"] = _normalise_confidence(raw)
    result["evidence"] = _normalise_evidence(raw, source_text, source_pages)

    subject = _clean_scalar(raw.get("subject"), limit=360)
    result["subject"] = subject or " - ".join(item for item in (title, document_type) if item)

    unknown = []
    scalar_fields = (
        "document_date", "title", "document_type", "issuer", "recipient", "owner",
        "amount", "currency",
    )
    unknown.extend(field for field in scalar_fields if not result.get(field))
    if not result["tags"]:
        unknown.append("tags")
    if result["language"] == "und":
        unknown.append("language")
    if not result["reference_ids"]:
        unknown.append("reference_ids")
    if not any((result["period"].get("start"), result["period"].get("end"), result["period"].get("label"))):
        unknown.append("period")
    result["unknown_fields"] = unknown
    return result


_METADATA_EVIDENCE_FIELDS = (
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

_TITLE_STOPWORDS = {
    "der", "die", "das", "den", "dem", "des", "ein", "eine", "einer",
    "und", "oder", "von", "vom", "zur", "zum", "fur", "fuer", "im", "in",
    "the", "a", "an", "of", "for", "to", "and",
}

_SEMANTIC_ROLE_LABELS = {
    "document_date": {
        "positive": (
            "Dokumentdatum", "Belegdatum", "Rechnungsdatum", "Ausstellungsdatum",
            "Ausgabedatum", "Vertragsdatum", "Briefdatum", "Schreibdatum",
            "Datum des Schreibens", "erstellt am", "ausgestellt am", "Datum",
        ),
        "negative": (
            "Fälligkeitsdatum", "Leistungsdatum", "Lieferdatum", "Geburtsdatum",
            "Buchungsdatum", "Zahlungsdatum", "Eingangsdatum", "Ablaufdatum",
            "gültig bis", "Zahlungsziel", "Zeitraum bis", "Zeitraum von",
        ),
    },
    "amount": {
        "positive": (
            "Gesamtbetrag", "Rechnungsbetrag", "Endbetrag", "Zahlbetrag",
            "zu zahlen", "offener Betrag", "Restbetrag", "Gesamtsumme",
            "Summe", "Total", "Amount due", "Balance due", "Betrag",
        ),
        "negative": (
            "Nettobetrag", "Netto", "Mehrwertsteuer", "MwSt", "Umsatzsteuer",
            "Steuerbetrag", "Einzelpreis", "Stückpreis", "Rabatt", "Anzahlung",
            "bereits gezahlt", "Gutschrift", "Versandkosten",
        ),
    },
}


def _phrase_pattern(value: Any) -> re.Pattern[str] | None:
    tokens = re.findall(r"\w+", _normalised_phrase(value), flags=re.UNICODE)
    if not tokens:
        return None
    body = r"[\W_]+".join(re.escape(token) for token in tokens)
    return re.compile(rf"(?<!\w){body}(?!\w)", flags=re.UNICODE)


def _contains_variant(source_text: str, value: Any) -> bool:
    pattern = _phrase_pattern(value)
    return bool(pattern and pattern.search(_normalised_phrase(source_text)))


def _date_variants(value: Any) -> list[str]:
    text = str(value or "").strip()
    match = re.fullmatch(r"(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?", text)
    if not match:
        return [text] if text else []
    year, month, day = match.groups()
    if day:
        return [
            f"{year}-{month}-{day}", f"{year}/{month}/{day}", f"{year}.{month}.{day}",
            f"{day}.{month}.{year}", f"{day}/{month}/{year}", f"{day}-{month}-{year}",
        ]
    if month:
        return [f"{year}-{month}", f"{year}/{month}", f"{month}.{year}", f"{month}/{year}"]
    return [year]


def _amount_variants(value: Any) -> list[str]:
    text = str(value or "").strip()
    if not text:
        return []
    try:
        amount = Decimal(text)
    except InvalidOperation:
        return [text]
    fixed = f"{amount:.2f}"
    integer, decimals = fixed.split(".")
    german_grouped = f"{int(integer):,}".replace(",", ".")
    english_grouped = f"{int(integer):,}"
    variants = {
        text,
        fixed,
        fixed.replace(".", ","),
        f"{german_grouped},{decimals}",
        f"{english_grouped}.{decimals}",
    }
    if decimals == "00":
        variants.update({integer, german_grouped, english_grouped})
    return sorted(variants)


def _tag_variants(tag: Any) -> list[str]:
    text = _clean_scalar(tag, limit=80)
    canonical_key = _ascii_key(text)
    variants = {text}
    for alias, canonical in _TAG_ALIASES.items():
        if _ascii_key(canonical) == canonical_key:
            variants.add(alias.replace("_", " "))
    return sorted(item for item in variants if item)


def _currency_variants(value: Any) -> list[str]:
    canonical = _normalise_currency(value)
    variants = {canonical}
    variants.update(alias for alias, mapped in _CURRENCY_ALIASES.items() if mapped == canonical)
    return sorted(item for item in variants if item)


def _title_is_supported(value: Any, source_text: str) -> bool:
    title = _clean_scalar(value, limit=240)
    if not title:
        return False
    if _contains_variant(source_text, title):
        return True
    tokens = [
        token
        for token in re.findall(r"\w+", _normalised_phrase(title), flags=re.UNICODE)
        if len(token) >= 3 and token not in _TITLE_STOPWORDS
    ]
    if not tokens:
        return False
    supported = sum(1 for token in tokens if _contains_variant(source_text, token))
    return supported == len(tokens) or (supported >= 2 and supported / len(tokens) >= 0.67)


def _scalar_variants(field: str, value: Any) -> list[str]:
    if field in {"document_date", "period"}:
        return _date_variants(value)
    if field == "amount":
        return _amount_variants(value)
    if field == "currency":
        return _currency_variants(value)
    if field in {"document_type", "tags"}:
        return _tag_variants(value)
    text = _clean_scalar(value, limit=500)
    return [text] if text else []


def _value_is_supported(field: str, value: Any, source_text: str) -> bool:
    if field == "title":
        return _title_is_supported(value, source_text)
    for variant in _scalar_variants(field, value):
        if variant in {"€", "$", "£"}:
            if variant in str(source_text or ""):
                return True
        elif _contains_variant(source_text, variant):
            return True
    return False


def _role_label_matches(text: str, labels: tuple[str, ...]) -> list[tuple[int, int, str]]:
    matches = []
    for label in labels:
        pattern = _phrase_pattern(label)
        if not pattern:
            continue
        for match in pattern.finditer(text):
            matches.append((match.start(), match.end(), label))
    return matches


def _nearby_semantic_roles(
    text: str,
    value_start: int,
    value_end: int,
    field: str,
) -> list[dict[str, Any]]:
    labels = _SEMANTIC_ROLE_LABELS[field]
    result = []
    for role in ("positive", "negative"):
        for start, end, label in _role_label_matches(text, labels[role]):
            if end <= value_start:
                distance = value_start - end
            elif start >= value_end:
                distance = start - value_end
            else:
                distance = 0
            # Labels normally precede values.  A short suffix is accepted for
            # tables, while distant labels must not bleed into another row.
            maximum = 80 if end <= value_start else 35
            if distance <= maximum:
                result.append({"role": role, "label": label, "distance": distance})
    return sorted(result, key=lambda item: (item["distance"], item["role"] != "negative"))


def _normalise_role_value(field: str, value: str) -> str:
    if field == "document_date":
        return _parse_date(value)[0]
    if field == "amount":
        return _normalise_amount(value)[0]
    return ""


def _positive_role_values(field: str, source_text: str) -> set[str]:
    """Collect distinct source values attached to authoritative field labels."""
    text = _normalised_phrase(source_text)
    if field == "document_date":
        pattern = re.compile(
            r"(?<!\w)(?:\d{1,2}[./-]\d{1,2}[./-]\d{2,4}|"
            r"\d{4}[./-]\d{1,2}[./-]\d{1,2})(?!\w)"
        )
    else:
        pattern = re.compile(
            r"(?<![\w.])[-+]?\d(?:[\d .']*\d)?[,.]\d{2}(?![\w.\d])"
        )
    result = set()
    for match in pattern.finditer(text):
        roles = _nearby_semantic_roles(text, match.start(), match.end(), field)
        nearest = roles[0] if roles else None
        if not nearest or nearest["role"] != "positive":
            continue
        normalised = _normalise_role_value(field, match.group(0))
        if normalised:
            result.add(normalised)
    return result


def _semantic_value_support(field: str, value: Any, source_text: str) -> dict[str, Any]:
    """Require date/amount values to occur next to their archival role label."""
    text = _normalised_phrase(source_text)
    occurrences: list[dict[str, Any]] = []
    spans = set()
    for variant in _scalar_variants(field, value):
        pattern = _phrase_pattern(variant)
        if not pattern:
            continue
        for match in pattern.finditer(text):
            span = (match.start(), match.end())
            if span in spans:
                continue
            spans.add(span)
            roles = _nearby_semantic_roles(text, match.start(), match.end(), field)
            nearest_distance = roles[0]["distance"] if roles else None
            nearest_roles = {
                item["role"] for item in roles if item["distance"] == nearest_distance
            }
            if not roles:
                status = "label_missing"
            elif len(nearest_roles) > 1:
                status = "ambiguous_role"
            else:
                status = next(iter(nearest_roles))
            occurrences.append({
                "variant": variant,
                "status": status,
                "labels": [item["label"] for item in roles[:4]],
            })

    statuses = {item["status"] for item in occurrences}
    positive_values = _positive_role_values(field, source_text)
    selected = _normalise_role_value(field, str(value or ""))
    ambiguous_values = bool(
        len(positive_values) > 1
        and selected in positive_values
    )
    supported = bool(
        "positive" in statuses
        and "negative" not in statuses
        and "ambiguous_role" not in statuses
        and not ambiguous_values
    )
    if supported:
        reason = "role_label"
    elif ambiguous_values or "ambiguous_role" in statuses or {"positive", "negative"} <= statuses:
        reason = "ambiguous_role"
    elif "negative" in statuses:
        reason = "wrong_role_label"
    elif occurrences:
        reason = "role_label_missing"
    else:
        reason = "value_missing"
    return {
        "supported": supported,
        "reason": reason,
        "occurrences": occurrences[:8],
        "positive_role_values": sorted(positive_values),
    }


def _usable_evidence_quotes(metadata: Mapping[str, Any], field: str) -> list[str]:
    evidence = metadata.get("evidence") if isinstance(metadata.get("evidence"), Mapping) else {}
    items = evidence.get(field) if isinstance(evidence.get(field), list) else []
    result = []
    for item in items:
        if not isinstance(item, Mapping) or not item.get("verified_in_text"):
            continue
        if item.get("page") and item.get("page_verified") is False:
            continue
        quote = _clean_scalar(item.get("quote"), limit=500)
        if quote:
            result.append(quote)
    return result


def _field_values(metadata: Mapping[str, Any], field: str) -> list[Any]:
    value = metadata.get(field)
    if field == "tags":
        return list(value) if isinstance(value, list) else []
    if field == "reference_ids":
        return [
            item.get("value") if isinstance(item, Mapping) else item
            for item in (value if isinstance(value, list) else [])
            if (item.get("value") if isinstance(item, Mapping) else item)
        ]
    if field == "period":
        if not isinstance(value, Mapping):
            return []
        return [value.get(key) for key in ("start", "end", "label") if value.get(key)]
    return [value] if value not in (None, "", [], {}) else []


def assess_metadata_evidence(
    metadata: Mapping[str, Any] | None,
    source_text: str,
    *,
    manually_confirmed: bool = False,
) -> dict[str, Any]:
    """Assess whether descriptive metadata is supported by document evidence.

    The report is intentionally separate from normalization: unverified machine
    suggestions remain visible for review, but cannot silently become an
    authoritative archival description.  A missing document date is valid and
    does not by itself require review.
    """
    metadata = metadata if isinstance(metadata, Mapping) else empty_metadata()
    fields: dict[str, dict[str, Any]] = {}
    unverified_fields: list[str] = []

    for field in _METADATA_EVIDENCE_FIELDS:
        values = _field_values(metadata, field)
        if not values:
            fields[field] = {"status": "unknown", "value_count": 0}
            continue
        if manually_confirmed:
            fields[field] = {
                "status": "human_confirmed",
                "value_count": len(values),
                "supported_count": len(values),
            }
            continue

        quotes = _usable_evidence_quotes(metadata, field)
        value_results = []
        for value in values:
            semantic = None
            if field in _SEMANTIC_ROLE_LABELS:
                semantic = _semantic_value_support(field, value, source_text)
                direct = bool(semantic["supported"])
                # A model-selected quote is still part of the same ambiguous
                # source and cannot resolve a wrong or competing semantic role.
                quoted = False
            else:
                direct = _value_is_supported(field, value, source_text)
                quoted = any(_value_is_supported(field, value, quote) for quote in quotes)
            result_item = {
                "value": value,
                "supported": bool(direct or quoted),
                "support": "source_text" if direct else "field_evidence" if quoted else "none",
            }
            if semantic is not None:
                result_item["semantic_role"] = semantic
            value_results.append(result_item)
        supported_count = sum(1 for item in value_results if item["supported"])
        status = "grounded" if supported_count == len(value_results) else "unverified"
        fields[field] = {
            "status": status,
            "value_count": len(values),
            "supported_count": supported_count,
            "values": value_results,
        }
        if status == "unverified":
            unverified_fields.append(field)

    descriptive_empty = not any(
        _field_values(metadata, field) for field in ("title", "document_type", "tags")
    )
    review_reasons = []
    warnings = []
    if unverified_fields and not manually_confirmed:
        message = "Unbelegte Maschinenmetadaten müssen geprüft werden: " + ", ".join(unverified_fields)
        warnings.append(message)
        review_reasons.append({
            "code": "metadata_values_unverified",
            "severity": "warning",
            "message": message,
            "fields": unverified_fields,
            "action": "Werte anhand der Originalseite bestätigen oder korrigieren.",
        })
    if descriptive_empty and not manually_confirmed:
        message = "Titel, Dokumenttyp und Tags fehlen vollständig; archivische Beschreibung prüfen."
        warnings.append(message)
        review_reasons.append({
            "code": "metadata_description_empty",
            "severity": "warning",
            "message": message,
            "fields": ["title", "document_type", "tags"],
            "action": "Mindestens Titel, Dokumenttyp oder geeignete Tags ergänzen.",
        })

    requires_review = bool(review_reasons)
    return {
        "schema_version": "unified_ocr_metadata_evidence_v1",
        "status": (
            "human_confirmed" if manually_confirmed else "review" if requires_review else "grounded"
        ),
        "manually_confirmed": bool(manually_confirmed),
        "requires_review": requires_review,
        "fields": fields,
        "unverified_fields": unverified_fields,
        "warnings": warnings,
        "review_reasons": review_reasons,
    }


def metadata_to_legacy(metadata: Any) -> dict[str, Any]:
    """Return scalar values for consumers that cannot yet accept tag lists."""
    normalised = normalize_metadata(metadata) if not isinstance(metadata, Mapping) or metadata.get("schema_version") != SCHEMA_VERSION else dict(metadata)
    legacy = dict(normalised)
    tags = normalised.get("tags") or []
    legacy["tags"] = ", ".join(str(tag) for tag in tags) if isinstance(tags, list) else str(tags)
    legacy.pop("schema_version", None)
    return legacy


def parse_metadata_response(response: Any) -> dict[str, Any] | None:
    """Parse JSON-like LLM output without accepting executable input."""
    if isinstance(response, Mapping):
        return dict(response)
    text = str(response or "").lstrip("\ufeff").strip()
    if not text:
        return None

    candidates = []
    fenced = re.findall(r"```(?:json)?\s*([\s\S]*?)```", text, flags=re.IGNORECASE)
    candidates.extend(fenced)
    candidates.append(text)

    for candidate in candidates:
        starts = [index for index, char in enumerate(candidate) if char == "{"]
        for start in starts:
            depth = 0
            in_string = False
            escaped = False
            for index in range(start, len(candidate)):
                char = candidate[index]
                if in_string:
                    if escaped:
                        escaped = False
                    elif char == "\\":
                        escaped = True
                    elif char == '"':
                        in_string = False
                    continue
                if char == '"':
                    in_string = True
                elif char == "{":
                    depth += 1
                elif char == "}":
                    depth -= 1
                    if depth == 0:
                        fragment = candidate[start:index + 1]
                        parsed = _parse_mapping_fragment(fragment)
                        if parsed is not None:
                            return parsed
                        break
    return None


def _parse_mapping_fragment(fragment: str) -> dict[str, Any] | None:
    variants = [
        fragment,
        re.sub(r",\s*([}\]])", r"\1", fragment),
    ]
    for value in variants:
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            parsed = None
        if isinstance(parsed, Mapping):
            return dict(parsed)
    try:
        parsed = ast.literal_eval(fragment)
    except (ValueError, SyntaxError):
        return None
    return dict(parsed) if isinstance(parsed, Mapping) else None


def build_document_excerpt(text: Any, *, max_chars: int = 12_000) -> str:
    """Return a deterministic head/middle/tail representation of a document."""
    value = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    max_chars = max(600, int(max_chars))
    if len(value) <= max_chars:
        return value

    marker_head = "[DOCUMENT BEGIN]\n"
    marker_mid = "\n\n[DOCUMENT MIDDLE]\n"
    marker_tail = "\n\n[DOCUMENT END]\n"
    available = max_chars - len(marker_head) - len(marker_mid) - len(marker_tail)
    head_size = int(available * 0.40)
    middle_size = int(available * 0.25)
    tail_size = available - head_size - middle_size
    middle_start = max(head_size, (len(value) - middle_size) // 2)
    middle_end = middle_start + middle_size
    return (
        marker_head + value[:head_size]
        + marker_mid + value[middle_start:middle_end]
        + marker_tail + value[-tail_size:]
    )


def metadata_tags_text(metadata: Mapping[str, Any] | None) -> str:
    """Render structured tags for PDF metadata, logs, and legacy UI widgets."""
    if not isinstance(metadata, Mapping):
        return ""
    tags = metadata.get("tags")
    if isinstance(tags, list):
        return ", ".join(_clean_scalar(tag, limit=80) for tag in tags if _clean_scalar(tag, limit=80))
    return _clean_scalar(tags, limit=1000)
