from core.quality import QualityChecker


def test_empty_final_result_is_a_blocking_review_case():
    report = QualityChecker.run_quality_check(
        "Quelltext mit Aktenzeichen 4711",
        "",
        "",
        "  \n\t",
    )

    assert report["severity"] == "error"
    assert report["quality_status"] == "critical"
    assert report["review_required"] is True
    assert report["review"]["blocking"] is True
    assert report["metrics"]["fused_empty"] is True
    assert "empty_final_text" in {
        reason["code"] for reason in report["review_reasons"]
    }


def test_amount_and_date_source_conflicts_are_not_treated_as_union_truth():
    report = QualityChecker.run_quality_check(
        "Betrag 100,00 EUR vom 01.02.2024",
        "Betrag 900,00 EUR vom 03.02.2024",
        "",
        "Betrag 100,00 EUR vom 01.02.2024",
    )

    conflict_types = {conflict["type"] for conflict in report["source_conflicts"]}
    assert conflict_types == {"amount", "date"}
    assert report["review_required"] is True
    assert report["missing_values"] == []
    assert report["extra_values"] == []
    assert report["field_checks"]["amount"]["unverified_output"] == ["100,00"]
    assert report["field_checks"]["date"]["unverified_output"] == ["01.02.2024"]


def test_conflicted_majority_value_is_manual_review_not_auto_correction():
    report = QualityChecker.run_quality_check(
        "Betrag 100,00 EUR",
        "Betrag 100,00 EUR",
        "Betrag 900,00 EUR",
        "Kein Betrag lesbar",
    )

    assert report["field_checks"]["amount"]["missing_expected"] == ["100,00"]
    assert report["missing_values"] == []
    assert any(
        reason["code"] == "amount_source_conflict"
        for reason in report["review_reasons"]
    )
    assert report["review"]["auto_correction_allowed"] is False


def test_output_only_amount_is_reported_symmetrically_as_critical_extra_value():
    report = QualityChecker.run_quality_check(
        "Rechnungsbetrag 100,00 EUR",
        "Rechnungsbetrag 100,00 EUR",
        "100,00 EUR",
        "Rechnungsbetrag 100,00 EUR; Zusatz 999,99 EUR",
    )

    assert report["severity"] == "error"
    assert report["quality_status"] == "critical"
    assert report["extra_values"] == [
        {
            "type": "amount",
            "value": "999,99",
            "normalized_value": "999,99",
            "supported_by": [],
        }
    ]
    assert any(
        reason["code"] == "unsupported_amount"
        for reason in report["review_reasons"]
    )


def test_digit_expansion_is_checked_even_without_amount_or_date_syntax():
    report = QualityChecker.run_quality_check(
        "Aktenzeichen 1234",
        "Aktenzeichen 1234",
        "Aktenzeichen 1234",
        "Aktenzeichen 1234 und erfunden 999999",
    )

    assert report["metrics"]["digit_ratio"] > 1.5
    assert report["quality_status"] == "critical"
    assert any(
        reason["code"] == "digit_expansion"
        for reason in report["review_reasons"]
    )


def test_equivalent_date_spellings_do_not_create_a_source_conflict():
    report = QualityChecker.run_quality_check(
        "Datum 20.06.2026",
        "Datum 2026-06-20",
        "Datum 20-06-2026",
        "Datum 20. Juni 2026",
    )

    assert report["source_conflicts"] == []
    assert report["quality_status"] == "ok"
    assert report["review_required"] is False


def test_long_narrative_text_loss_is_detected_even_without_number_loss():
    source = (
        "Dies ist ein ausführlicher Vertrag mit mehreren wichtigen Absätzen "
        "über Leistungen Laufzeit Kündigung Datenschutz Haftung Gewährleistung "
        "Ansprechpartner Zuständigkeiten Fristen Anlagen Voraussetzungen und "
        "gegenseitige Pflichten der beteiligten Parteien. Der zweite Abschnitt "
        "beschreibt Lieferung Abnahme Dokumentation Wartung Support Eskalation "
        "Verfügbarkeit Sicherheit und Nachweise."
    )
    fused = "Dies ist ein ausführlicher Vertrag über Leistungen und Laufzeit."

    report = QualityChecker.run_quality_check(source, source, "", fused)

    assert report["quality_status"] == "critical"
    assert report["metrics"]["token_recall"] < 0.65
    assert any(
        reason["code"] == "text_coverage_loss"
        for reason in report["review_reasons"]
    )


def test_equivalent_long_text_with_layout_changes_keeps_full_coverage():
    source = " ".join(f"Begriff{index}" for index in range(30))
    fused = "\n\n".join(source.split())

    report = QualityChecker.run_quality_check(source, source, "", fused)

    assert report["metrics"]["token_recall"] == 1.0
    assert not any(
        reason["code"].startswith("text_coverage")
        for reason in report["review_reasons"]
    )
