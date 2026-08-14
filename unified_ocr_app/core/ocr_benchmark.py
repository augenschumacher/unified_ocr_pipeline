"""Reproducible OCR quality metrics for user-maintained golden corpora."""

from __future__ import annotations

import argparse
import json
import re
import unicodedata
from pathlib import Path
from typing import Iterable, Sequence

from core.metadata import normalize_metadata, normalize_tags


def normalize_benchmark_text(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = text.replace("\u00ad", "")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def levenshtein_distance(reference: Sequence, candidate: Sequence) -> int:
    """Memory-bounded Levenshtein distance for characters or word tokens."""
    if len(reference) < len(candidate):
        reference, candidate = candidate, reference
    previous = list(range(len(candidate) + 1))
    for row, ref_value in enumerate(reference, start=1):
        current = [row]
        for column, candidate_value in enumerate(candidate, start=1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[column] + 1,
                    previous[column - 1] + (ref_value != candidate_value),
                )
            )
        previous = current
    return previous[-1]


def char_error_rate(reference: str, candidate: str) -> float:
    reference_text = normalize_benchmark_text(reference)
    candidate_text = normalize_benchmark_text(candidate)
    denominator = max(1, len(reference_text))
    return levenshtein_distance(reference_text, candidate_text) / denominator


def word_error_rate(reference: str, candidate: str) -> float:
    reference_words = normalize_benchmark_text(reference).split()
    candidate_words = normalize_benchmark_text(candidate).split()
    denominator = max(1, len(reference_words))
    return levenshtein_distance(reference_words, candidate_words) / denominator


def _fact_key(value: str) -> str:
    text = normalize_benchmark_text(value).casefold()
    return re.sub(r"[\s\u00a0]+", "", text)


def critical_value_recall(expected_values: Iterable[str], candidate: str) -> tuple[float, list[str]]:
    expected = [str(value) for value in expected_values if str(value).strip()]
    if not expected:
        return 1.0, []
    haystack = _fact_key(candidate)
    missing = [value for value in expected if _fact_key(value) not in haystack]
    return (len(expected) - len(missing)) / len(expected), missing


def _canonical_value(value) -> str:
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).casefold()
    if isinstance(value, (list, tuple, set)):
        return json.dumps(list(value), ensure_ascii=False, sort_keys=True, default=str).casefold()
    return normalize_benchmark_text(str(value or "")).casefold()


def _evaluate_structured(case: dict, candidate_record: dict | None, candidate_text: str) -> dict:
    expected_tags = normalize_tags(case.get("expected_tags"))
    expected_metadata = case.get("expected_metadata") if isinstance(case.get("expected_metadata"), dict) else {}
    expected_target = str(case.get("expected_target_path") or "").strip().replace("\\", "/")
    expected = bool(expected_tags or expected_metadata or expected_target)
    if not expected:
        return {"expected": False, "candidate_present": candidate_record is not None}
    if not isinstance(candidate_record, dict):
        return {"expected": True, "candidate_present": False}

    raw_metadata = (
        candidate_record.get("metadata")
        if isinstance(candidate_record.get("metadata"), dict)
        else candidate_record
    )
    actual_metadata = normalize_metadata(raw_metadata, source_text=candidate_text)
    expected_normalized = normalize_metadata(expected_metadata, source_text=str(case.get("reference_text") or ""))
    actual_tags = normalize_tags(actual_metadata.get("tags"))
    expected_tag_keys = {tag.casefold() for tag in expected_tags}
    actual_tag_keys = {tag.casefold() for tag in actual_tags}
    tag_intersection = len(expected_tag_keys & actual_tag_keys)
    tag_precision = tag_intersection / max(1, len(actual_tag_keys)) if expected_tags else None
    tag_recall = tag_intersection / max(1, len(expected_tag_keys)) if expected_tags else None
    tag_f1 = (
        2 * tag_precision * tag_recall / max(tag_precision + tag_recall, 1e-12)
        if expected_tags
        else None
    )

    checked_fields = []
    matched_fields = []
    for field in expected_metadata:
        if field in {"tags", "raw_tags", "tag_keys", "field_confidence", "evidence"}:
            continue
        checked_fields.append(field)
        if _canonical_value(actual_metadata.get(field)) == _canonical_value(expected_normalized.get(field)):
            matched_fields.append(field)

    classification = (
        candidate_record.get("classification")
        if isinstance(candidate_record.get("classification"), dict)
        else {}
    )
    actual_target = str(
        candidate_record.get("target_path")
        or classification.get("recommended_path")
        or ""
    ).strip().replace("\\", "/")
    folder_match = (
        actual_target.casefold() == expected_target.casefold()
        if expected_target
        else None
    )
    return {
        "expected": True,
        "candidate_present": True,
        "expected_tags": expected_tags,
        "actual_tags": actual_tags,
        "tag_precision": round(tag_precision, 6) if tag_precision is not None else None,
        "tag_recall": round(tag_recall, 6) if tag_recall is not None else None,
        "tag_f1": round(tag_f1, 6) if tag_f1 is not None else None,
        "metadata_checked_fields": checked_fields,
        "metadata_matched_fields": matched_fields,
        "metadata_accuracy": (
            round(len(matched_fields) / len(checked_fields), 6)
            if checked_fields
            else None
        ),
        "expected_target_path": expected_target or None,
        "actual_target_path": actual_target or None,
        "folder_match": folder_match,
    }


def evaluate_case(case: dict, candidate_text: str, candidate_record: dict | None = None) -> dict:
    reference = str(case.get("reference_text") or "")
    expected_values = case.get("critical_values") or []
    value_recall, missing_values = critical_value_recall(expected_values, candidate_text)
    result = {
        "id": str(case.get("id") or "case"),
        "reference_chars": len(normalize_benchmark_text(reference)),
        "candidate_chars": len(normalize_benchmark_text(candidate_text)),
        "cer": round(char_error_rate(reference, candidate_text), 6),
        "wer": round(word_error_rate(reference, candidate_text), 6),
        "critical_value_recall": round(value_recall, 6),
        "missing_critical_values": missing_values,
    }
    result["structured"] = _evaluate_structured(case, candidate_record, candidate_text)
    return result


def evaluate_corpus(cases: list[dict], candidate_dir: Path) -> dict:
    candidate_dir = Path(candidate_dir)
    results = []
    missing_candidates = []
    missing_structured_candidates = []
    for case in cases:
        case_id = str(case.get("id") or "").strip()
        if not case_id or not re.fullmatch(r"[A-Za-z0-9_.-]+", case_id):
            raise ValueError(f"Ungültige Golden-Corpus-ID: {case_id!r}")
        candidate_path = candidate_dir / f"{case_id}.txt"
        if not candidate_path.is_file():
            missing_candidates.append(str(candidate_path))
            continue
        candidate_text = candidate_path.read_text(encoding="utf-8", errors="replace")
        structured_expected = bool(
            case.get("expected_tags")
            or case.get("expected_metadata")
            or case.get("expected_target_path")
        )
        structured_path = candidate_dir / f"{case_id}.json"
        candidate_record = None
        if structured_path.is_file():
            try:
                parsed = json.loads(structured_path.read_text(encoding="utf-8"))
                if isinstance(parsed, dict):
                    candidate_record = parsed
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                candidate_record = None
        if structured_expected and candidate_record is None:
            missing_structured_candidates.append(str(structured_path))
        result = evaluate_case(case, candidate_text, candidate_record)
        result["candidate_path"] = str(candidate_path)
        if candidate_record is not None:
            result["structured_candidate_path"] = str(structured_path)
        results.append(result)

    total_reference_chars = sum(result["reference_chars"] for result in results)
    total_candidate_chars = sum(result["candidate_chars"] for result in results)
    weighted_cer = (
        sum(result["cer"] * result["reference_chars"] for result in results)
        / max(1, total_reference_chars)
    )
    mean_wer = sum(result["wer"] for result in results) / max(1, len(results))
    mean_recall = (
        sum(result["critical_value_recall"] for result in results)
        / max(1, len(results))
    )
    structured_results = [
        result.get("structured", {})
        for result in results
        if (result.get("structured") or {}).get("expected")
    ]
    tag_scores = [item["tag_f1"] for item in structured_results if item.get("tag_f1") is not None]
    metadata_checked = sum(len(item.get("metadata_checked_fields") or []) for item in structured_results)
    metadata_matched = sum(len(item.get("metadata_matched_fields") or []) for item in structured_results)
    folder_results = [item for item in structured_results if item.get("folder_match") is not None]
    return {
        "schema": "unified_ocr_golden_benchmark_v1",
        "case_count": len(cases),
        "evaluated_count": len(results),
        "missing_candidates": missing_candidates,
        "missing_structured_candidates": missing_structured_candidates,
        "metrics": {
            "weighted_cer": round(weighted_cer, 6),
            "mean_wer": round(mean_wer, 6),
            "mean_critical_value_recall": round(mean_recall, 6),
            "reference_chars": total_reference_chars,
            "candidate_chars": total_candidate_chars,
            "mean_tag_f1": round(sum(tag_scores) / len(tag_scores), 6) if tag_scores else None,
            "metadata_accuracy": (
                round(metadata_matched / metadata_checked, 6)
                if metadata_checked
                else None
            ),
            "folder_accuracy": (
                round(sum(bool(item["folder_match"]) for item in folder_results) / len(folder_results), 6)
                if folder_results
                else None
            ),
        },
        "cases": results,
    }


def load_corpus(path: Path) -> tuple[list[dict], dict]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(payload, list):
        cases, thresholds = payload, {}
    elif isinstance(payload, dict):
        cases = payload.get("cases") or []
        thresholds = payload.get("thresholds") or {}
    else:
        raise ValueError("Golden Corpus muss ein JSON-Objekt oder eine Liste sein.")
    if not isinstance(cases, list) or not all(isinstance(case, dict) for case in cases):
        raise ValueError("Golden Corpus enthält keine gültige Fallliste.")
    return cases, thresholds if isinstance(thresholds, dict) else {}


def benchmark_passed(report: dict, thresholds: dict) -> tuple[bool, list[str]]:
    metrics = report.get("metrics") or {}
    failures = []
    max_cer = float(thresholds.get("max_weighted_cer", 0.03))
    max_wer = float(thresholds.get("max_mean_wer", 0.08))
    min_recall = float(thresholds.get("min_critical_value_recall", 1.0))
    if report.get("missing_candidates"):
        failures.append(f"{len(report['missing_candidates'])} Kandidatendatei(en) fehlen.")
    if report.get("missing_structured_candidates"):
        failures.append(
            f"{len(report['missing_structured_candidates'])} strukturierte Kandidatendatei(en) fehlen."
        )
    if float(metrics.get("weighted_cer", 1.0)) > max_cer:
        failures.append(f"CER {metrics.get('weighted_cer')} > {max_cer}")
    if float(metrics.get("mean_wer", 1.0)) > max_wer:
        failures.append(f"WER {metrics.get('mean_wer')} > {max_wer}")
    if float(metrics.get("mean_critical_value_recall", 0.0)) < min_recall:
        failures.append(
            f"Wertetreffer {metrics.get('mean_critical_value_recall')} < {min_recall}"
        )
    for threshold_key, metric_key, label in (
        ("min_mean_tag_f1", "mean_tag_f1", "Tag-F1"),
        ("min_metadata_accuracy", "metadata_accuracy", "Metadaten-Trefferquote"),
        ("min_folder_accuracy", "folder_accuracy", "Ordner-Trefferquote"),
    ):
        if threshold_key not in thresholds:
            continue
        metric_value = metrics.get(metric_key)
        minimum = float(thresholds[threshold_key])
        if metric_value is None or float(metric_value) < minimum:
            failures.append(f"{label} {metric_value} < {minimum}")
    return not failures, failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Unified OCR Golden-Corpus-Benchmark")
    parser.add_argument("--corpus", required=True, type=Path, help="Golden-Corpus-JSON")
    parser.add_argument(
        "--candidate-dir",
        required=True,
        type=Path,
        help="Ordner mit <case-id>.txt OCR-Ergebnissen",
    )
    parser.add_argument("--report", type=Path, help="Optionaler JSON-Berichtspfad")
    args = parser.parse_args(argv)

    cases, thresholds = load_corpus(args.corpus)
    report = evaluate_corpus(cases, args.candidate_dir)
    passed, failures = benchmark_passed(report, thresholds)
    report["passed"] = passed
    report["failures"] = failures
    serialized = json.dumps(report, indent=2, ensure_ascii=False)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(serialized + "\n", encoding="utf-8")
    print(serialized)
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
