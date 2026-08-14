from core.cloud.classification_memory import ClassificationMemory
from core.cloud.classifier import classify_document
from core.cloud.context_matcher import rank_context_paths
from core.llm.tasks import LLMClient
from core.metadata import (
    assess_metadata_evidence,
    build_document_excerpt,
    normalize_tags,
    normalize_metadata,
    parse_metadata_response,
)


def test_metadata_evidence_is_alias_and_format_aware_without_requiring_a_date():
    source = (
        "Invoice ACME GmbH. Gesamtbetrag 1.234,50 EUR. "
        "Diese Rechnung betrifft den Vertrag."
    )
    metadata = normalize_metadata(
        {
            "document_date": None,
            "title": "Invoice ACME",
            "document_type": "Rechnung",
            "tags": ["Rechnung", "Vertrag"],
            "issuer": "ACME GmbH",
            "amount": "1234.50",
            "currency": "EUR",
        },
        source_text=source,
    )

    report = assess_metadata_evidence(metadata, source)

    assert report["requires_review"] is False
    assert report["fields"]["document_date"]["status"] == "unknown"
    assert report["fields"]["document_type"]["status"] == "grounded"
    assert report["fields"]["tags"]["supported_count"] == 2
    assert report["fields"]["amount"]["status"] == "grounded"
    assert report["fields"]["currency"]["status"] == "grounded"


def test_metadata_evidence_rejects_unrelated_quote_and_unverified_page_claim():
    metadata = normalize_metadata(
        {
            "title": "Invoice",
            "document_type": "Rechnung",
            "tags": ["Rechnung"],
            "owner": "Alice",
            "evidence": {"owner": [{"quote": "Invoice", "page": 2}]},
        },
        source_text="Invoice",
        source_pages={1: "Invoice"},
    )

    owner_evidence = metadata["evidence"]["owner"][0]
    report = assess_metadata_evidence(metadata, "Invoice")

    assert owner_evidence["verified_in_text"] is True
    assert owner_evidence["page_verified"] is False
    assert owner_evidence["page_error"] == "page_not_available"
    assert report["fields"]["owner"]["status"] == "unverified"
    assert report["requires_review"] is True


def test_metadata_page_claim_is_not_verified_without_page_texts():
    metadata = normalize_metadata(
        {
            "title": "Rechnung",
            "document_type": "Rechnung",
            "tags": ["Rechnung"],
            "evidence": {"title": [{"quote": "Rechnung", "page": 1}]},
        },
        source_text="Rechnung",
    )

    evidence = metadata["evidence"]["title"][0]

    assert evidence["verified_in_text"] is True
    assert evidence["page_verified"] is False
    assert evidence["page_error"] == "page_verification_unavailable"


def test_human_confirmed_metadata_is_not_blocked_by_machine_grounding():
    metadata = normalize_metadata(
        {"title": "Manueller Titel", "document_type": "Sondertyp", "tags": ["Spezial"]},
        source_text="abweichender OCR-Text",
    )

    report = assess_metadata_evidence(
        metadata,
        "abweichender OCR-Text",
        manually_confirmed=True,
    )

    assert report["requires_review"] is False
    assert report["status"] == "human_confirmed"
    assert report["fields"]["title"]["status"] == "human_confirmed"


def test_metadata_schema_keeps_missing_date_unknown_and_normalises_legacy_values():
    source = "Vertragsdatum nicht angegeben. Vertragsnummer AB-123. Gesamtbetrag 1.234,50 EUR."
    metadata = normalize_metadata(
        {
            "date": "heute",
            "titel": "Jahresabrechnung 2025",
            "typ": "Abrechnung",
            "tags": "Finanzen, Vertrag; finanzen",
            "absender": "Beispielwerke GmbH",
            "empfaenger": "Fabio Schumacher",
            "eigentuemer": "Fabio Schumacher",
            "sprache": "Deutsch",
            "reference_ids": {"Vertragsnummer": "AB-123"},
            "zeitraum": {"von": "01.01.2025", "bis": "31.12.2025"},
            "betrag": "1.234,50 EUR",
            "field_confidence": {"datum": 85, "absender": 0.91},
            "evidence": {
                "reference_ids": {"quote": "Vertragsnummer AB-123"},
                "amount": {"quote": "Gesamtbetrag 1.234,50 EUR"},
            },
        },
        source_text=source,
    )

    assert metadata["document_date"] is None
    assert metadata["date"] == ""
    assert metadata["date_status"] == "unknown"
    assert metadata["tags"] == ["Finanzen", "Vertrag"]
    assert metadata["tag_keys"] == ["finanzen", "vertrag"]
    assert metadata["tags_text"] == "Finanzen, Vertrag"
    assert metadata["issuer"] == "Beispielwerke GmbH"
    assert metadata["recipient"] == "Fabio Schumacher"
    assert metadata["owner"] == "Fabio Schumacher"
    assert metadata["language"] == "de"
    assert metadata["reference_ids"] == [{"type": "vertragsnummer", "value": "AB-123"}]
    assert metadata["period"]["start"] == "2025-01-01"
    assert metadata["period"]["end"] == "2025-12-31"
    assert metadata["amount"] == "1234.5"
    assert metadata["currency"] == "EUR"
    assert metadata["field_confidence"]["document_date"] == 0.85
    assert metadata["evidence"]["amount"][0]["verified_in_text"] is True


def test_tags_use_conservative_vocabulary_without_losing_specific_terms():
    tags = normalize_tags(
        [
            "Invoice",
            "rechnung",
            "Dokument",
            "AB-123",
            "Photovoltaik Anlage",
            "Photovoltaik Anlage",
            "dies ist ein viel zu langer beschreibender Satz als Tag",
        ]
    )

    assert tags == ["Rechnung", "AB-123", "Photovoltaik Anlage"]


def test_metadata_response_parser_accepts_fences_single_quotes_and_trailing_comma():
    parsed = parse_metadata_response(
        "Vorwort\n```json\n{'date': '31.12.2025', 'tags': 'Steuer; Bescheid',}\n```"
    )
    metadata = normalize_metadata(parsed)

    assert metadata["document_date"] == "2025-12-31"
    assert metadata["date"] == "31-12-2025"
    assert metadata["tags"] == ["Steuer", "Bescheid"]


def test_document_excerpt_deterministically_includes_beginning_middle_and_end():
    text = "BEGIN-ANCHOR\n" + ("A" * 7000) + "\nMIDDLE-ANCHOR\n" + ("B" * 7000) + "\nEND-ANCHOR"
    excerpt = build_document_excerpt(text, max_chars=1200)

    assert len(excerpt) == 1200
    assert "BEGIN-ANCHOR" in excerpt
    assert "MIDDLE-ANCHOR" in excerpt
    assert "END-ANCHOR" in excerpt
    assert "[DOCUMENT MIDDLE]" in excerpt


def test_analysis_normalises_legacy_llm_json_and_uses_neutral_contract():
    captured = {}
    client = LLMClient(vision_model="v", fusion_model="f", analysis_model="analysis")

    def fake_query(model, system_prompt, user_prompt, *args, **kwargs):
        captured["system"] = system_prompt
        captured["user"] = user_prompt
        return '{"date": null, "title": "Vertrag", "document_type": "Vertrag", "tags": "Vertrag; Recht", "sender": "ACME"}'

    client.query = fake_query
    result = client.run_analysis("Anfang " + ("x" * 14000) + " Schluss ACME")

    assert result["date"] == ""
    assert result["tags"] == ["Vertrag", "Recht"]
    assert result["issuer"] == "ACME"
    assert "NIEMALS durch das heutige Datum" in captured["system"]
    assert "medizinischer Archivar" not in captured["system"]
    assert "Schluss ACME" in captured["user"]


def test_context_matching_uses_boundaries_for_short_terms():
    known = ["Fabio/Auto/HU"]
    contexts = {known[0]: {"keywords": ["HU"]}}

    assert rank_context_paths("Neue Schuhe gekauft", {}, known, contexts) == []
    matches = rank_context_paths("HU am Fahrzeug bestanden", {}, known, contexts)
    assert matches
    assert matches[0]["evidence"] == ["keywords:HU"]


def test_hallucinated_metadata_cannot_become_automatic_folder_evidence():
    class NoModel:
        analysis_model = None
        fusion_model = None

    known = ["Fabio/Auto/Golf"]
    contexts = {
        known[0]: {
            "object_type": "vehicle",
            "aliases": ["AB-CD-123"],
            "keywords": ["Golf 7", "Inspektion"],
        }
    }
    hallucinated = normalize_metadata(
        {
            "owner": "Fabio",
            "recipient": "Fabio",
            "tags": ["Golf 7", "Inspektion"],
            "reference_ids": [{"type": "vehicle", "value": "AB-CD-123"}],
        },
        source_text="Neutraler Kontoauszug ohne Fahrzeugdaten.",
    )

    matches = rank_context_paths(
        "Neutraler Kontoauszug ohne Fahrzeugdaten.",
        hallucinated,
        known,
        contexts,
    )

    assert matches == []

    result = classify_document(
        "Neutraler Kontoauszug ohne Fahrzeugdaten.",
        hallucinated,
        known,
        NoModel(),
        ["Fabio"],
        path_contexts=contexts,
    )
    assert result["auto_assign"] is False
    assert result["review_required"] is True
    assert result["abstained"] is True


def test_source_grounded_identifier_enables_context_auto_assignment():
    class NoModel:
        analysis_model = None
        fusion_model = None

    known = ["Fabio/Auto/Golf"]
    contexts = {
        known[0]: {
            "object_type": "vehicle",
            "aliases": ["AB-CD-123"],
            "keywords": ["Golf 7"],
        }
    }
    text = "Werkstattbeleg fuer Golf 7, Kennzeichen AB CD 123."
    metadata = normalize_metadata(
        {
            "owner": "Fabio",
            "tags": ["Golf 7"],
            "reference_ids": [{"type": "vehicle", "value": "AB-CD-123"}],
        },
        source_text=text,
    )

    result = classify_document(
        text,
        metadata,
        known,
        NoModel(),
        ["Fabio"],
        path_contexts=contexts,
    )

    assert result["recommended_path"] == known[0]
    assert result["reason"] == "context_match"
    assert result["auto_assign"] is True
    assert "context_owner_binding:Fabio" in result["owner_evidence"]


def test_learning_memory_keeps_only_source_grounded_metadata_terms(tmp_path):
    memory = ClassificationMemory(tmp_path)
    hallucinated = {
        "document_type": "Inspektion",
        "owner": "Fabio",
        "recipient": "Fabio",
        "tags": ["Golf 7"],
        "reference_ids": [{"type": "vehicle", "value": "AB-CD-123"}],
    }

    memory.record_decision(
        chosen_path="Fabio/Auto/Golf",
        fused_text="Neutraler Kontoauszug ohne Fahrzeugdaten.",
        metadata=hallucinated,
        source="manual_review",
    )

    terms = memory.data["decisions"][-1]["terms"]
    assert not any(term.startswith(("doctype:", "tag:", "party_", "id_vehicle:")) for term in terms)

    memory.record_decision(
        chosen_path="Fabio/Auto/Golf",
        fused_text="Fabio: Inspektion fuer Golf 7 mit Kennzeichen AB CD 123.",
        metadata=hallucinated,
        source="manual_review",
    )
    grounded_terms = memory.data["decisions"][-1]["terms"]
    assert "doctype:inspektion" in grounded_terms
    assert "tag:golf 7" in grounded_terms
    assert "party_owner:fabio" in grounded_terms
    assert "id_vehicle:ab-cd-123" in grounded_terms


def test_llm_classification_is_review_proposal_not_automatic_truth_and_sees_tail():
    captured = {}

    class MockLLM:
        analysis_model = "mock-model"
        fusion_model = None

        def query(self, model, system_prompt, user_prompt, **kwargs):
            captured["user"] = user_prompt
            return (
                '{"recommended_path":"Fabio/Finanzen/Vertrag",'
                '"confidence":99,"reason":"Vertrag erkannt",'
                '"evidence":["TAIL_OWNER Fabio"]}'
            )

    document = "HEAD\n" + ("neutral " * 3000) + "\nTAIL_OWNER Fabio"
    result = classify_document(
        document,
        {"document_type": "Vertrag"},
        ["Fabio/Finanzen/Vertrag", "Sonstiges"],
        MockLLM(),
        ["Fabio", "Sonstiges"],
    )

    assert "TAIL_OWNER Fabio" in captured["user"]
    assert result["recommended_path"] == "Fabio/Finanzen/Vertrag"
    assert result["model_confidence"] == 99
    assert result["confidence"] < 60
    assert result["review_required"] is True
    assert result["auto_assign"] is False
    assert result["evidence_details"][0]["verified_in_text"] is True
    assert result["owner_evidence"] == ["text_person:Fabio"]


def test_classifier_explicitly_abstains_without_model_or_evidence():
    class NoModel:
        analysis_model = None
        fusion_model = None

    result = classify_document("unbestimmter Inhalt", {}, ["Sonstiges"], NoModel(), ["Sonstiges"])

    assert result["decision"] == "abstain"
    assert result["abstained"] is True
    assert result["review_required"] is True
    assert result["confidence"] == 0


def test_learning_memory_ignores_predictions_and_can_undo_confirmation(tmp_path):
    memory = ClassificationMemory(tmp_path)
    ignored = memory.record_decision(
        chosen_path="Fabio/Auto/Golf",
        fused_text="Golf 7 AB CD 123",
        metadata={"document_type": "Service"},
        source="llm",
    )

    assert ignored is False
    assert memory.data["decisions"] == []

    recorded = memory.record_decision(
        chosen_path="Fabio/Auto/Golf",
        fused_text="Golf 7 Kennzeichen AB CD 123 Inspektion",
        metadata={"document_type": "Service", "tags": ["Fahrzeug", "Wartung"]},
        source="manual_review",
    )
    assert recorded is True
    assert memory.data["decisions"][-1]["confirmed"] is True
    assert any(term.startswith("id_vehicle:") for term in memory.data["decisions"][-1]["terms"])

    removed = memory.undo_last_decision()
    assert removed["chosen_path"] == "Fabio/Auto/Golf"
    assert memory.data["decisions"] == []
    assert memory.data["path_stats"] == {}
