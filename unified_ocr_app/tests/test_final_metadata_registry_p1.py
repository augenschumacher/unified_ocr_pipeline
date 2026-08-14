import json

import pytest

from core.cloud.classification_memory import ClassificationMemory
from core.cloud.context_matcher import rank_context_paths
from core.cloud.folder_registry import FolderRegistry, RegistryWriteError
from core.metadata import assess_metadata_evidence, normalize_metadata


def _registry_payload(*, revision=7):
    return {
        "revision": revision,
        "persons": ["Fabio"],
        "known_paths": ["Fabio", "Fabio/Finanzen"],
        "drive_folders": {},
        "path_contexts": {},
    }


def test_corrupt_registry_primary_restores_validated_backup_without_overwriting_it(tmp_path):
    primary = tmp_path / "folder_registry.json"
    backup = tmp_path / "folder_registry.backup.json"
    primary.write_text('{"revision": 99, broken', encoding="utf-8")
    backup_bytes = (json.dumps(_registry_payload(), ensure_ascii=False) + "\n").encode("utf-8")
    backup.write_bytes(backup_bytes)

    registry = FolderRegistry(tmp_path)

    assert registry.get_known_paths() == ["Fabio", "Fabio/Finanzen"]
    assert backup.read_bytes() == backup_bytes
    assert json.loads(primary.read_text(encoding="utf-8"))["revision"] == 7
    quarantined = list(tmp_path.glob("folder_registry.corrupt.*.json"))
    assert len(quarantined) == 1
    assert b"broken" in quarantined[0].read_bytes()


def test_corrupt_registry_and_invalid_backup_fail_closed_and_preserve_evidence(tmp_path):
    primary = tmp_path / "folder_registry.json"
    backup = tmp_path / "folder_registry.backup.json"
    primary_bytes = b"{not-json"
    backup_bytes = b"[]"
    primary.write_bytes(primary_bytes)
    backup.write_bytes(backup_bytes)

    with pytest.raises(RegistryWriteError, match="keine gueltige Sicherung"):
        FolderRegistry(tmp_path)

    assert primary.read_bytes() == primary_bytes
    assert backup.read_bytes() == backup_bytes
    assert any(path.read_bytes() == primary_bytes for path in tmp_path.glob("folder_registry.corrupt.*.json"))
    with pytest.raises(RegistryWriteError):
        FolderRegistry(tmp_path)


def test_memory_blocks_cross_person_auto_routing_even_with_matching_identifier(tmp_path):
    memory = ClassificationMemory(tmp_path)
    training_text = "Fabio, Kundennummer AB-12345, Versicherungsvertrag Jahresbeitrag."
    for index in range(3):
        memory.record_decision(
            chosen_path="Fabio/Versicherung/Vertrag",
            fused_text=training_text,
            metadata={"owner": "Fabio", "reference_ids": [{"type": "customer", "value": "AB-12345"}]},
            source="manual_review",
            decision_id=f"training-{index}",
        )

    current_text = "Empfängerin Lara. Kundennummer AB-12345. Versicherungsvertrag Jahresbeitrag."
    candidates = memory.build_candidates(
        current_text,
        {"recipient": "Lara", "reference_ids": [{"type": "customer", "value": "AB-12345"}]},
        ["Fabio/Versicherung/Vertrag"],
    )

    assert candidates
    assert candidates[0]["party_conflict"] is True
    assert candidates[0]["auto_assign_eligible"] is False
    assert any(item.startswith("conflicting_recipient:lara") for item in candidates[0]["owner_evidence"])


def test_memory_person_target_requires_person_or_stable_identifier_evidence(tmp_path):
    memory = ClassificationMemory(tmp_path)
    text = "Versicherungsdokument Jahresbeitrag Vertragsunterlagen Leistungsübersicht."
    for index in range(3):
        memory.record_decision(
            chosen_path="Fabio/Versicherung/Vertrag",
            fused_text=text,
            metadata={"document_type": "Versicherungsdokument"},
            source="manual_review",
            decision_id=f"generic-{index}",
        )

    candidates = memory.build_candidates(
        text,
        {"document_type": "Versicherungsdokument"},
        ["Fabio/Versicherung/Vertrag"],
    )

    assert candidates
    assert candidates[0]["auto_assign_eligible"] is False
    assert not candidates[0]["owner_evidence"]


def test_plain_context_alias_does_not_create_owner_binding_without_opt_in():
    path = "Fabio/Gesundheit/Akte"
    context = {
        "aliases": ["Praxis Sonnenschein", "Gesundheitsakte Privat"],
        "keywords": ["Vorsorgeuntersuchung"],
    }
    text = "Praxis Sonnenschein, Gesundheitsakte Privat, Vorsorgeuntersuchung."

    candidate = rank_context_paths(text, {}, [path], {path: context})[0]

    assert candidate["owner_evidence"] == []
    assert candidate["auto_assign_eligible"] is False
    assert not any(item.startswith("context_owner_binding:") for item in candidate["evidence"])


def test_context_owner_binding_requires_explicit_opt_in_or_stable_identifier(tmp_path):
    path = "Fabio/Gesundheit/Akte"
    opted_in = rank_context_paths(
        "Praxis Sonnenschein Gesundheitsakte Privat",
        {},
        [path],
        {path: {"aliases": ["Praxis Sonnenschein", "Gesundheitsakte Privat"], "binds_owner": True}},
    )[0]
    stable_id = rank_context_paths(
        "Patientenakte Kennung AB CD 123",
        {},
        [path],
        {path: {"aliases": ["AB-CD-123"]}},
    )[0]

    assert opted_in["owner_evidence"] == ["context_owner_binding:Fabio"]
    assert stable_id["owner_evidence"] == ["context_owner_binding:Fabio"]

    registry = FolderRegistry(tmp_path)
    registry.add_person("Fabio")
    registry.add_path(path)
    registry.set_path_context(path, {"aliases": ["Praxis Sonnenschein"], "binds_owner": True})
    assert FolderRegistry(tmp_path).get_path_context(path)["binds_owner"] is True


def _base_metadata(**values):
    raw = {
        "title": "Rechnung",
        "document_type": "Rechnung",
        "tags": ["Rechnung"],
        **values,
    }
    return raw


def test_document_date_with_due_date_label_is_not_accepted_as_document_date():
    source = (
        "Rechnung. Rechnungsdatum: 01.02.2026. "
        "Fälligkeitsdatum: 15.02.2026. Gesamtbetrag: 119,00 EUR."
    )
    metadata = normalize_metadata(
        _base_metadata(document_date="2026-02-15", amount="119.00", currency="EUR"),
        source_text=source,
    )

    report = assess_metadata_evidence(metadata, source)
    date_result = report["fields"]["document_date"]["values"][0]

    assert report["fields"]["document_date"]["status"] == "unverified"
    assert date_result["semantic_role"]["reason"] == "wrong_role_label"
    assert report["requires_review"] is True


def test_net_amount_with_wrong_label_is_not_accepted_as_archival_total():
    source = "Rechnung. Nettobetrag: 100,00 EUR. MwSt: 19,00 EUR. Gesamtbetrag: 119,00 EUR."
    metadata = normalize_metadata(
        _base_metadata(amount="100.00", currency="EUR"),
        source_text=source,
    )

    report = assess_metadata_evidence(metadata, source)
    amount_result = report["fields"]["amount"]["values"][0]

    assert report["fields"]["amount"]["status"] == "unverified"
    assert amount_result["semantic_role"]["reason"] == "wrong_role_label"
    assert report["requires_review"] is True


def test_multiple_plausible_total_amount_roles_require_review():
    source = "Rechnung. Gesamtbetrag: 119,00 EUR. Zu zahlen: 99,00 EUR."
    metadata = normalize_metadata(
        _base_metadata(amount="119.00", currency="EUR"),
        source_text=source,
    )

    report = assess_metadata_evidence(metadata, source)
    semantic = report["fields"]["amount"]["values"][0]["semantic_role"]

    assert report["fields"]["amount"]["status"] == "unverified"
    assert semantic["reason"] == "ambiguous_role"
    assert semantic["positive_role_values"] == ["119", "99"]
    assert report["requires_review"] is True
