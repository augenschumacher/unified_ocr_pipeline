"""Evidence-oriented OCR quality gates.

The checker treats OCR, Docling and vision output as independent evidence. It
does not promote the union of all extracted values to truth. Values reported by
several channels form consensus; disagreements are surfaced for review and are
never returned as automatic correction candidates.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from datetime import datetime


logger = logging.getLogger("UnifiedOCR")

_SEVERITY_RANK = {"info": 0, "warning": 1, "error": 2}
_AMOUNT_PATTERN = re.compile(
    r"(?<![\d.,])[-+\u2212]?(?:\d{1,3}(?:[.\s]\d{3})+|\d+),\d{2}(?!\d)"
)
_DATE_PATTERNS = (
    re.compile(r"\b\d{1,2}\.\d{1,2}\.\d{4}\b"),
    re.compile(r"\b\d{1,2}-\d{1,2}-\d{4}\b"),
    re.compile(r"\b\d{4}-\d{1,2}-\d{1,2}\b"),
    re.compile(
        r"\b\d{1,2}\.\s*(?:Januar|Februar|Maerz|März|April|Mai|Juni|"
        r"Juli|August|September|Oktober|November|Dezember)\s+\d{4}\b",
        flags=re.IGNORECASE,
    ),
)
_GERMAN_MONTHS = {
    "januar": 1,
    "februar": 2,
    "maerz": 3,
    "märz": 3,
    "april": 4,
    "mai": 5,
    "juni": 6,
    "juli": 7,
    "august": 8,
    "september": 9,
    "oktober": 10,
    "november": 11,
    "dezember": 12,
}


class QualityChecker:
    @staticmethod
    def normalize_amount(amount_str: str) -> str:
        """Normalize German-formatted money amounts for comparison."""
        value = (
            str(amount_str or "")
            .replace(".", "")
            .replace(" ", "")
            .replace("\u00a0", "")
            .replace("\u2212", "-")
        )
        if value.startswith("+"):
            value = value[1:]
        return value

    @classmethod
    def extract_amounts(cls, text: str) -> set[str]:
        """Extract amounts such as 123,45, 1234,56 and 1.234.567,89."""
        return {
            cls.normalize_amount(match.group(0))
            for match in _AMOUNT_PATTERN.finditer(str(text or ""))
        }

    @staticmethod
    def extract_dates(text: str) -> set[str]:
        """Extract common numeric and German long date forms."""
        dates: set[str] = set()
        for pattern in _DATE_PATTERNS:
            dates.update(match.group(0) for match in pattern.finditer(str(text or "")))
        return dates

    @staticmethod
    def normalize_date(date_str: str) -> str:
        """Normalize equivalent supported date spellings to ISO format."""
        raw = re.sub(r"\s+", " ", str(date_str or "")).strip()
        for date_format in ("%d.%m.%Y", "%d-%m-%Y", "%Y-%m-%d"):
            try:
                return datetime.strptime(raw, date_format).date().isoformat()
            except ValueError:
                pass

        match = re.fullmatch(r"(\d{1,2})\.\s*([A-Za-zÄÖÜäöü]+)\s+(\d{4})", raw)
        if match:
            day, month_name, year = match.groups()
            month = _GERMAN_MONTHS.get(month_name.casefold())
            if month:
                try:
                    return datetime(int(year), month, int(day)).date().isoformat()
                except ValueError:
                    pass
        return raw.casefold()

    @staticmethod
    def extract_la_codes(text: str) -> set[str]:
        """Extract likely German payroll code numbers."""
        candidates = re.findall(r"\b\d{3,4}\b", str(text or ""))
        return {
            candidate
            for candidate in candidates
            if not (1900 <= int(candidate) <= 2100)
        }

    @classmethod
    def _semantic_digit_count(cls, text: str) -> int:
        """Count digits after normalizing equivalent date representations.

        A German long date has two fewer literal digits than its numeric form
        although it carries the same fact. Replacing recognized dates with
        YYYYMMDD prevents that harmless formatting change from looking like
        numeric data loss.
        """
        normalized = str(text or "")
        for raw_date in sorted(cls.extract_dates(normalized), key=len, reverse=True):
            canonical = cls.normalize_date(raw_date)
            replacement = canonical.replace("-", "")
            if re.fullmatch(r"\d{8}", replacement):
                normalized = normalized.replace(raw_date, replacement)
        return sum(character.isdigit() for character in normalized)

    @staticmethod
    def _word_tokens(text: str) -> list[str]:
        """Return layout-insensitive lexical tokens for document coverage."""
        return [
            token.casefold()
            for token in re.findall(r"(?u)\b[^\W_]+(?:[-'][^\W_]+)*\b", str(text or ""))
            if len(token) >= 2
        ]

    @staticmethod
    def _meaningful(text: str) -> bool:
        return any(character.isalnum() for character in str(text or ""))

    @staticmethod
    def _raise_severity(current: str, candidate: str) -> str:
        if _SEVERITY_RANK[candidate] > _SEVERITY_RANK[current]:
            return candidate
        return current

    @classmethod
    def _raw_amount_map(cls, text: str) -> dict[str, str]:
        return {
            cls.normalize_amount(match.group(0)): match.group(0)
            for match in _AMOUNT_PATTERN.finditer(str(text or ""))
        }

    @classmethod
    def _normalized_dates(cls, text: str) -> tuple[set[str], dict[str, str]]:
        raw_dates = cls.extract_dates(text)
        display = {cls.normalize_date(value): value for value in raw_dates}
        return set(display), display

    @staticmethod
    def _analyse_field(
        field_type: str,
        source_values: dict[str, set[str]],
        output_values: set[str],
        display_values: dict[str, str],
    ) -> dict:
        """Build consensus and disagreement facts without assuming union truth."""
        reporting_sources = {
            source: set(values)
            for source, values in source_values.items()
            if values
        }
        support: dict[str, list[str]] = {}
        for source, values in reporting_sources.items():
            for value in values:
                support.setdefault(value, []).append(source)

        if len(reporting_sources) == 1:
            expected_values = set(next(iter(reporting_sources.values())))
        elif len(reporting_sources) > 1:
            expected_values = {
                value for value, sources in support.items() if len(sources) >= 2
            }
        else:
            expected_values = set()

        source_union = set(support)
        distinct_sets = {frozenset(values) for values in reporting_sources.values()}
        has_conflict = len(reporting_sources) >= 2 and len(distinct_sets) > 1
        unverified_output = (
            (output_values & source_union) - expected_values if has_conflict else set()
        )

        def shown(value: str) -> str:
            return display_values.get(value, value)

        candidates = [
            {
                "value": shown(value),
                "normalized_value": value,
                "sources": sorted(sources),
                "support_count": len(sources),
            }
            for value, sources in sorted(support.items())
        ]
        return {
            "type": field_type,
            "status": "conflict" if has_conflict else "ok",
            "source_values": {
                source: [shown(value) for value in sorted(values)]
                for source, values in source_values.items()
            },
            "candidates": candidates,
            "expected_values": [shown(value) for value in sorted(expected_values)],
            "output_values": [shown(value) for value in sorted(output_values)],
            "missing_expected": [
                shown(value) for value in sorted(expected_values - output_values)
            ],
            "unsupported_output": [
                shown(value) for value in sorted(output_values - source_union)
            ],
            "unverified_output": [
                shown(value) for value in sorted(unverified_output)
            ],
            "_expected": expected_values,
            "_source_union": source_union,
            "_output": set(output_values),
            "_support": support,
            "_conflict": has_conflict,
        }

    @staticmethod
    def quality_score(severity: str, warnings: list[str], metrics: dict) -> int:
        """Return a conservative, explainable score for UI sorting."""
        score = 100
        if severity == "error":
            score -= 35
        elif severity == "warning":
            score -= 15
        score -= min(45, len(warnings) * 7)

        digit_ratio = metrics.get("digit_ratio")
        if isinstance(digit_ratio, (int, float)):
            if digit_ratio < 0.8:
                score -= min(25, int((0.8 - digit_ratio) * 100))
            elif digit_ratio > 1.25:
                score -= min(25, int((digit_ratio - 1.25) * 40))
        if metrics.get("fused_empty"):
            score -= 20
        token_recall = metrics.get("token_recall")
        if isinstance(token_recall, (int, float)) and token_recall < 0.85:
            score -= min(25, int((0.85 - token_recall) * 80))
        token_precision = metrics.get("token_precision")
        if isinstance(token_precision, (int, float)) and token_precision < 0.75:
            score -= min(20, int((0.75 - token_precision) * 70))
        score -= min(15, int(metrics.get("source_conflict_count", 0)) * 5)
        return max(0, min(100, int(score)))

    @staticmethod
    def quality_status(score: int, severity: str) -> str:
        if severity == "error" or score < 60:
            return "critical"
        if severity == "warning" or score < 85:
            return "review"
        return "ok"

    @classmethod
    def run_quality_check(
        cls,
        ocr_text: str,
        docling_markdown: str,
        vision_markdown: str,
        fused_text: str,
    ) -> dict:
        """Compare independent source evidence with the final text.

        The returned missing_values list contains only non-conflicting expected
        values. Conflicting candidates stay in source_conflicts and require
        manual review. Added output facts are reported symmetrically through
        extra_values.
        """
        source_texts = {
            "ocr": str(ocr_text or ""),
            "docling": str(docling_markdown or ""),
            "vision": str(vision_markdown or ""),
        }
        fused_text = str(fused_text or "")
        warnings: list[str] = []
        missing_values: list[dict] = []
        extra_values: list[dict] = []
        source_conflicts: list[dict] = []
        review_reasons: list[dict] = []
        severity = "info"

        def add_issue(code: str, level: str, message: str, **details) -> None:
            nonlocal severity
            warnings.append(message)
            reason = {"code": code, "severity": level, "message": message}
            reason.update(details)
            review_reasons.append(reason)
            severity = cls._raise_severity(severity, level)

        fused_empty = not cls._meaningful(fused_text)
        sources_empty = not any(cls._meaningful(text) for text in source_texts.values())
        if fused_empty:
            add_issue(
                "empty_final_text",
                "error",
                "Das Enddokument enthält keinen verwertbaren Text.",
                action="OCR-Ergebnis und Quelldokument manuell prüfen.",
            )
        if sources_empty:
            add_issue(
                "empty_source_evidence",
                "error",
                "Keiner der OCR-/Analysekanäle enthält verwertbaren Quelltext.",
                action="Eingabedatei, OCR-Sprachen und Vorverarbeitung prüfen.",
            )

        source_amounts = {
            source: cls.extract_amounts(text) for source, text in source_texts.items()
        }
        fused_amounts = cls.extract_amounts(fused_text)
        amount_display: dict[str, str] = {}
        for text in (*source_texts.values(), fused_text):
            amount_display.update(cls._raw_amount_map(text))
        amount_check = cls._analyse_field(
            "amount", source_amounts, fused_amounts, amount_display
        )

        source_dates: dict[str, set[str]] = {}
        date_display: dict[str, str] = {}
        for source, text in source_texts.items():
            values, displays = cls._normalized_dates(text)
            source_dates[source] = values
            date_display.update(displays)
        fused_dates, fused_date_display = cls._normalized_dates(fused_text)
        date_display.update(fused_date_display)
        date_check = cls._analyse_field(
            "date", source_dates, fused_dates, date_display
        )

        is_payroll = any(
            keyword in " ".join((*source_texts.values(), fused_text)).lower()
            for keyword in ("abrechnung", "gehalt", "lohn", "verdienst")
        )
        field_checks = {"amount": amount_check, "date": date_check}
        if is_payroll:
            source_la = {
                source: cls.extract_la_codes(text)
                for source, text in source_texts.items()
            }
            fused_la = cls.extract_la_codes(fused_text)
            la_display = {
                value: value
                for values in (*source_la.values(), fused_la)
                for value in values
            }
            field_checks["la_code"] = cls._analyse_field(
                "la_code", source_la, fused_la, la_display
            )

        labels = {
            "amount": ("Geldbetrag", " EUR"),
            "date": ("Datum", ""),
            "la_code": ("Möglicher Lohnarten-Code", ""),
        }
        for field_type, check in field_checks.items():
            label, suffix = labels[field_type]
            if check["_conflict"]:
                public_conflict = {
                    key: value
                    for key, value in check.items()
                    if not key.startswith("_")
                }
                public_conflict["requires_manual_review"] = True
                source_conflicts.append(public_conflict)
                candidates = ", ".join(
                    f"{candidate['value']} ({'/'.join(candidate['sources'])})"
                    for candidate in check["candidates"]
                )
                add_issue(
                    f"{field_type}_source_conflict",
                    "warning",
                    f"Quellenkonflikt bei {label}: {candidates}.",
                    field_type=field_type,
                    candidates=check["candidates"],
                    action="Wert anhand der Originalseite bestätigen.",
                )

            missing_normalized = check["_expected"] - check["_output"]
            for value in sorted(missing_normalized):
                readable = (
                    amount_display.get(value, value)
                    if field_type == "amount"
                    else date_display.get(value, value)
                    if field_type == "date"
                    else value
                )
                support = sorted(check["_support"].get(value, []))
                message = f"{label} fehlt im Enddokument: {readable}{suffix}"
                add_issue(
                    f"missing_{field_type}",
                    "warning",
                    message,
                    field_type=field_type,
                    value=readable,
                    supported_by=support,
                    action="Fundstelle im Original prüfen und manuell bestätigen.",
                )
                # A source conflict blocks automatic correction. This prevents
                # the pipeline from inserting several incompatible candidates.
                if not check["_conflict"]:
                    missing_values.append(
                        {
                            "type": field_type,
                            "value": readable,
                            "normalized_value": value,
                            "supported_by": support,
                            "evidence_level": (
                                "corroborated" if len(support) >= 2 else "single_source"
                            ),
                        }
                    )

            unsupported_normalized = check["_output"] - check["_source_union"]
            for value in sorted(unsupported_normalized):
                readable = (
                    amount_display.get(value, value)
                    if field_type == "amount"
                    else date_display.get(value, value)
                    if field_type == "date"
                    else value
                )
                message = (
                    f"{label} im Enddokument ist durch keine Quelle belegt: "
                    f"{readable}{suffix}"
                )
                add_issue(
                    f"unsupported_{field_type}",
                    "error",
                    message,
                    field_type=field_type,
                    value=readable,
                    action="Wert entfernen oder anhand der Originalseite bestätigen.",
                )
                extra_values.append(
                    {
                        "type": field_type,
                        "value": readable,
                        "normalized_value": value,
                        "supported_by": [],
                    }
                )

        raw_source_digit_counts = {
            source: sum(character.isdigit() for character in text)
            for source, text in source_texts.items()
        }
        source_digit_counts = {
            source: cls._semantic_digit_count(text)
            for source, text in source_texts.items()
        }
        source_digits_count = max(source_digit_counts.values(), default=0)
        fused_digits_count = cls._semantic_digit_count(fused_text)
        raw_fused_digits_count = sum(
            character.isdigit() for character in fused_text
        )
        if source_digits_count:
            digit_ratio: float | None = fused_digits_count / source_digits_count
            if digit_ratio < 0.80:
                add_issue(
                    "digit_loss",
                    "error",
                    "Hoher Ziffernverlust im Enddokument! "
                    f"Nur {digit_ratio:.1%} der Ziffern der vollständigsten Quelle "
                    f"erhalten ({fused_digits_count} von {source_digits_count}).",
                    source_digits=source_digits_count,
                    fused_digits=fused_digits_count,
                )
            elif digit_ratio > 1.25:
                level = "error" if digit_ratio > 1.50 else "warning"
                add_issue(
                    "digit_expansion",
                    level,
                    "Ungewöhnlich viele zusätzliche Ziffern im Enddokument: "
                    f"{digit_ratio:.1%} gegenüber der vollständigsten Quelle "
                    f"({fused_digits_count} statt {source_digits_count}).",
                    source_digits=source_digits_count,
                    fused_digits=fused_digits_count,
                )
        else:
            digit_ratio = None
            if fused_digits_count:
                add_issue(
                    "unsupported_digits",
                    "error",
                    "Das Enddokument enthält Ziffern, obwohl keine Quelle Ziffern belegt.",
                    fused_digits=fused_digits_count,
                )

        source_table_lines = sum(
            1 for line in source_texts["docling"].splitlines() if "|" in line
        )
        fused_table_lines = sum(1 for line in fused_text.splitlines() if "|" in line)
        if source_table_lines > 5 and fused_table_lines < source_table_lines * 0.7:
            add_issue(
                "table_line_loss",
                "warning",
                "Verlust von Tabellenzeilen vermutet: "
                f"Quell-Markdown hat {source_table_lines} Tabellenzeilen, "
                f"das fusionierte Dokument nur {fused_table_lines}.",
                source_table_lines=source_table_lines,
                fused_table_lines=fused_table_lines,
            )

        source_token_lists = {
            source: cls._word_tokens(text)
            for source, text in source_texts.items()
        }
        reference_source, reference_tokens = max(
            source_token_lists.items(),
            key=lambda item: len(item[1]),
            default=("", []),
        )
        fused_tokens = cls._word_tokens(fused_text)
        token_recall: float | None = None
        token_precision: float | None = None
        token_intersection = 0
        # Very short texts are too sensitive to harmless wording and heading
        # normalization.  Long-document coverage is the archival risk this
        # gate is intended to catch.
        if len(reference_tokens) >= 20:
            reference_counts = Counter(reference_tokens)
            fused_counts = Counter(fused_tokens)
            token_intersection = sum(
                min(count, fused_counts.get(token, 0))
                for token, count in reference_counts.items()
            )
            token_recall = token_intersection / len(reference_tokens)
            token_precision = token_intersection / max(1, len(fused_tokens))
            if token_recall < 0.65:
                add_issue(
                    "text_coverage_loss",
                    "error",
                    "Großer Textverlust im Enddokument vermutet: "
                    f"nur {token_recall:.1%} Wortabdeckung gegenüber der vollständigsten Quelle ({reference_source}).",
                    reference_source=reference_source,
                    reference_tokens=len(reference_tokens),
                    fused_tokens=len(fused_tokens),
                    token_recall=round(token_recall, 3),
                )
            elif token_recall < 0.80:
                add_issue(
                    "text_coverage_reduced",
                    "warning",
                    "Reduzierte Textabdeckung im Enddokument: "
                    f"{token_recall:.1%} gegenüber der vollständigsten Quelle ({reference_source}).",
                    reference_source=reference_source,
                    reference_tokens=len(reference_tokens),
                    fused_tokens=len(fused_tokens),
                    token_recall=round(token_recall, 3),
                )
            if token_precision < 0.55 and len(fused_tokens) > len(reference_tokens):
                add_issue(
                    "text_expansion_unverified",
                    "error",
                    "Große unbelegte Texterweiterung im Enddokument vermutet: "
                    f"nur {token_precision:.1%} der Ausgabetokens werden durch die vollständigste Quelle gestützt.",
                    reference_source=reference_source,
                    reference_tokens=len(reference_tokens),
                    fused_tokens=len(fused_tokens),
                    token_precision=round(token_precision, 3),
                )

        metrics = {
            "source_digits": source_digits_count,
            "source_digit_counts": source_digit_counts,
            "raw_source_digit_counts": raw_source_digit_counts,
            "fused_digits": fused_digits_count,
            "raw_fused_digits": raw_fused_digits_count,
            "digit_ratio": round(digit_ratio, 3) if digit_ratio is not None else None,
            "digit_loss_ratio": (
                round(max(0.0, 1.0 - digit_ratio), 3)
                if digit_ratio is not None
                else None
            ),
            "digit_expansion_ratio": (
                round(max(0.0, digit_ratio - 1.0), 3)
                if digit_ratio is not None
                else (1.0 if fused_digits_count else 0.0)
            ),
            "source_table_lines": source_table_lines,
            "fused_table_lines": fused_table_lines,
            "fused_empty": fused_empty,
            "sources_empty": sources_empty,
            "source_conflict_count": len(source_conflicts),
            "extra_value_count": len(extra_values),
            "reference_text_source": reference_source or None,
            "reference_tokens": len(reference_tokens),
            "fused_tokens": len(fused_tokens),
            "token_intersection": token_intersection,
            "token_recall": round(token_recall, 3) if token_recall is not None else None,
            "token_precision": round(token_precision, 3) if token_precision is not None else None,
        }
        score = cls.quality_score(severity, warnings, metrics)
        status = cls.quality_status(score, severity)
        review_required = status != "ok"

        public_field_checks = {
            field_type: {
                key: value for key, value in check.items() if not key.startswith("_")
            }
            for field_type, check in field_checks.items()
        }
        logger.info(
            "Qualitaetskontrolle abgeschlossen. Severity: %s, Warnungen: %s, "
            "Konflikte: %s, Score: %s",
            severity,
            len(warnings),
            len(source_conflicts),
            score,
        )

        return {
            "severity": severity,
            "quality_status": status,
            "quality_score": score,
            "warnings": warnings,
            "missing_values": missing_values,
            "extra_values": extra_values,
            "source_conflicts": source_conflicts,
            "field_checks": public_field_checks,
            "review_required": review_required,
            "requires_review": review_required,
            "review_reasons": review_reasons,
            "review": {
                "required": review_required,
                "blocking": status == "critical",
                "reasons": review_reasons,
                "auto_correction_allowed": False,
            },
            "metrics": metrics,
        }
