from core.quality import QualityChecker


def test_quality_report_contains_score_and_status_for_clean_text():
    report = QualityChecker.run_quality_check(
        "Rechnungsbetrag 1.234,56 EUR vom 20.05.2026.",
        "| Wert |\n|---|\n| 1.234,56 |",
        "Rechnung 1.234,56 EUR.",
        "Rechnung 1.234,56 EUR vom 20.05.2026.",
    )

    assert report["severity"] == "info"
    assert report["quality_status"] == "ok"
    assert report["quality_score"] == 100


def test_quality_checker_detects_common_date_formats():
    dates = QualityChecker.extract_dates("20.06.2026 20-06-2026 2026-06-20 1. Januar 2026")

    assert "20.06.2026" in dates
    assert "20-06-2026" in dates
    assert "2026-06-20" in dates
    assert "1. Januar 2026" in dates


def test_quality_report_marks_digit_loss_as_critical():
    report = QualityChecker.run_quality_check(
        "Betrag 1.234,56 EUR vom 20.05.2026.",
        "Tabelle | 1.234,56 |",
        "1.234,56",
        "Rechnung vom 20.05.2026.",
    )

    assert report["severity"] == "error"
    assert report["quality_status"] == "critical"
    assert report["quality_score"] < 85
