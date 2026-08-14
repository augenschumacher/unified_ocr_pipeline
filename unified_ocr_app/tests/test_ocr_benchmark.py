import json

from core.ocr_benchmark import (
    benchmark_passed,
    char_error_rate,
    critical_value_recall,
    evaluate_corpus,
    levenshtein_distance,
    load_corpus,
    word_error_rate,
)


def test_edit_metrics_and_critical_values_are_deterministic():
    assert levenshtein_distance("kitten", "sitting") == 3
    assert char_error_rate("Betrag 123,45 EUR", "Betrag 123,45 EUR") == 0
    assert word_error_rate("eins zwei drei", "eins X drei") == 1 / 3
    recall, missing = critical_value_recall(
        ["1.234,56 EUR", "RE-2026-00417"],
        "Gesamt 1.234,56\u00a0EUR; Nummer RE-2026-00417",
    )
    assert recall == 1
    assert missing == []


def test_example_corpus_can_gate_candidate_results(tmp_path):
    corpus_path = tmp_path / "corpus.json"
    corpus_path.write_text(
        json.dumps(
            {
                "thresholds": {
                    "max_weighted_cer": 0.05,
                    "max_mean_wer": 0.2,
                    "min_critical_value_recall": 1.0,
                },
                "cases": [
                    {
                        "id": "case-1",
                        "reference_text": "Rechnung RE-42 vom 12.07.2026 über 99,50 EUR",
                        "critical_values": ["RE-42", "12.07.2026", "99,50 EUR"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    candidate_dir = tmp_path / "candidates"
    candidate_dir.mkdir()
    (candidate_dir / "case-1.txt").write_text(
        "Rechnung RE-42 vom 12.07.2026 über 99,50 EUR",
        encoding="utf-8",
    )

    cases, thresholds = load_corpus(corpus_path)
    report = evaluate_corpus(cases, candidate_dir)
    passed, failures = benchmark_passed(report, thresholds)

    assert passed is True
    assert failures == []
    assert report["metrics"]["weighted_cer"] == 0
    assert report["metrics"]["mean_critical_value_recall"] == 1


def test_missing_candidate_is_a_hard_benchmark_failure(tmp_path):
    report = evaluate_corpus(
        [{"id": "missing", "reference_text": "Text", "critical_values": []}],
        tmp_path,
    )
    passed, failures = benchmark_passed(report, {})
    assert passed is False
    assert any("fehlen" in failure for failure in failures)


def test_structured_tags_metadata_and_folder_are_release_gated(tmp_path):
    cases = [
        {
            "id": "invoice",
            "reference_text": "Rechnung RE-42 vom 12.07.2026 über 99,50 EUR",
            "critical_values": ["RE-42", "12.07.2026", "99,50 EUR"],
            "expected_tags": ["Rechnung", "Finanzen"],
            "expected_metadata": {
                "document_date": "2026-07-12",
                "document_type": "Rechnung",
                "amount": "99.50",
                "currency": "EUR",
            },
            "expected_target_path": "Fabio/Finanzen/Rechnungen",
        }
    ]
    (tmp_path / "invoice.txt").write_text(cases[0]["reference_text"], encoding="utf-8")
    (tmp_path / "invoice.json").write_text(
        json.dumps(
            {
                "metadata": {
                    "document_date": "2026-07-12",
                    "document_type": "Rechnung",
                    "tags": ["Invoice", "Finanzen"],
                    "amount": "99,50 EUR",
                    "currency": "EUR",
                },
                "target_path": "Fabio/Finanzen/Rechnungen",
            }
        ),
        encoding="utf-8",
    )

    report = evaluate_corpus(cases, tmp_path)
    passed, failures = benchmark_passed(
        report,
        {
            "min_mean_tag_f1": 1.0,
            "min_metadata_accuracy": 1.0,
            "min_folder_accuracy": 1.0,
        },
    )

    assert passed is True
    assert failures == []
    assert report["metrics"]["mean_tag_f1"] == 1.0
    assert report["metrics"]["metadata_accuracy"] == 1.0
    assert report["metrics"]["folder_accuracy"] == 1.0


def test_missing_structured_candidate_is_a_hard_failure(tmp_path):
    case = {
        "id": "structured",
        "reference_text": "Vertrag AB-1",
        "expected_tags": ["Vertrag"],
    }
    (tmp_path / "structured.txt").write_text("Vertrag AB-1", encoding="utf-8")

    report = evaluate_corpus([case], tmp_path)
    passed, failures = benchmark_passed(report, {"min_mean_tag_f1": 1.0})

    assert passed is False
    assert report["missing_structured_candidates"]
    assert any("strukturierte" in failure for failure in failures)
