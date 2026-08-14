import json
from pathlib import Path
from unittest.mock import MagicMock

from core.cloud.classification_memory import ClassificationMemory
from core.cloud.classifier import classify_document
from core.cloud.folder_registry import FolderRegistry
from core.config import AppConfig
from core.pipeline import PipelineOrchestrator


def test_classification_memory_learns_confirmed_vehicle_path(tmp_path):
    memory = ClassificationMemory(tmp_path)
    text = "Rechnung Autohaus Mueller fuer Golf 7 Kennzeichen AB CD 123 Inspektion."

    memory.record_decision(
        chosen_path="Fabio/Auto/Golf/Service",
        fused_text=text,
        metadata={"document_type": "Service"},
        proposed_path="Fabio/Finanzen/Rechnungen",
        candidates=[{"path": "Fabio/Finanzen/Rechnungen", "score": 62}],
        source="test",
    )

    candidates = memory.build_candidates(
        "Autohaus Mueller bestaetigt die Inspektion fuer Golf 7 AB CD 123.",
        {"document_type": "Service"},
        ["Fabio/Auto/Golf/Service", "Fabio/Finanzen/Rechnungen"],
    )

    assert candidates
    assert candidates[0]["path"] == "Fabio/Auto/Golf/Service"
    assert candidates[0]["reason"] == "memory"


def test_classifier_uses_high_confidence_memory_candidate_without_llm():
    class MockLLM:
        analysis_model = "mock-model"
        fusion_model = None

        def query(self, *args, **kwargs):
            raise AssertionError("High-confidence memory candidate should avoid LLM query")

    res = classify_document(
        "Golf 7 mit Kennzeichen AB CD 123",
        {},
        ["Fabio/Auto/Golf/Service"],
        MockLLM(),
        ["Fabio"],
        memory_candidates=[{
            "path": "Fabio/Auto/Golf/Service",
            "score": 92,
            "reason": "memory",
            "evidence": ["object:golf 7", "id_vehicle:ab cd 123"],
            "confirmed_count": 2,
            "auto_assign_eligible": True,
        }],
    )

    assert res["recommended_path"] == "Fabio/Auto/Golf/Service"
    assert res["reason"] == "memory"
    assert res["confidence"] == 92


def test_classifier_rejects_legacy_memory_evidence_missing_from_source():
    class NoModel:
        analysis_model = None
        fusion_model = None

    res = classify_document(
        "Neutraler Kontoauszug ohne Fahrzeugdaten.",
        {"owner": "Fabio", "tags": ["Golf 7"]},
        ["Fabio/Auto/Golf/Service"],
        NoModel(),
        ["Fabio"],
        memory_candidates=[{
            "path": "Fabio/Auto/Golf/Service",
            "score": 96,
            "reason": "memory",
            "evidence": ["party_owner:fabio", "tag:golf 7"],
            "confirmed_count": 4,
            "auto_assign_eligible": True,
        }],
    )

    assert res["auto_assign"] is False
    assert res["review_required"] is True
    assert res["abstained"] is True


def test_stage_organize_prompts_on_close_candidates_and_records_learning(tmp_path):
    config = AppConfig(tmp_path)
    config.ensure_directories()
    registry = FolderRegistry(tmp_path)
    registry.save_tree({"Jan": {"Auto": {"Golf": {}, "Tesla": {}}}})

    final_name = "2026-06-09_Service"
    source_file = config.final_dir / f"{final_name}.pdf"
    source_file.write_text("pdf", encoding="utf-8")

    llm = MagicMock()
    llm.run_classification.return_value = {
        "recommended_path": "Jan/Auto/Golf",
        "is_new": False,
        "confidence": 62,
        "reason": "llm",
        "candidates": [
            {"path": "Jan/Auto/Golf", "score": 62, "reason": "llm", "evidence": ["service"]},
            {"path": "Jan/Auto/Tesla", "score": 58, "reason": "memory", "evidence": ["autohaus"]},
        ],
    }
    prompt = MagicMock(return_value="Jan/Auto/Tesla")

    orch = PipelineOrchestrator(
        config=config,
        llm_client=llm,
        prompt_sorting_callback=prompt,
    )

    moved, target_path = orch._stage_organize(
        "Autohaus Rechnung Service Inspektion Model 3",
        {"document_type": "Service"},
        final_name,
        preview_pdf_path=source_file,
    )

    assert target_path == "Jan/Auto/Tesla"
    assert prompt.called
    assert prompt.call_args.args[3] == source_file
    assert moved
    assert config.final_dir.joinpath("Jan", "Auto", "Tesla", source_file.name).exists()

    data = json.loads(tmp_path.joinpath("classification_memory.json").read_text(encoding="utf-8"))
    assert data["decisions"][-1]["chosen_path"] == "Jan/Auto/Tesla"
    assert data["decisions"][-1]["source"] == "sorting_prompt"


def test_stage_organize_always_prompts_and_accepts_full_final_path(tmp_path):
    config = AppConfig(tmp_path)
    config.ensure_directories()
    registry = FolderRegistry(tmp_path)
    registry.save_tree({"Jan": {"Auto": {"Golf": {}}}})

    final_name = "2026-06-10_Service"
    source_file = config.final_dir / f"{final_name}.pdf"
    source_file.write_text("pdf", encoding="utf-8")

    llm = MagicMock()
    llm.run_classification.return_value = {
        "recommended_path": "Jan/Auto/Golf",
        "is_new": False,
        "confidence": 96,
        "reason": "llm",
        "candidates": [
            {"path": "Jan/Auto/Golf", "score": 96, "reason": "llm", "evidence": ["service"]},
        ],
    }
    prompt = MagicMock(return_value=str(config.final_dir / "Jan" / "Auto" / "Tesla"))

    orch = PipelineOrchestrator(
        config=config,
        llm_client=llm,
        prompt_sorting_callback=prompt,
        confirm_sorting_each_document=True,
    )

    moved, target_path = orch._stage_organize(
        "Autohaus Rechnung Service",
        {"document_type": "Service"},
        final_name,
        preview_pdf_path=source_file,
    )

    assert prompt.called
    assert prompt.call_args.args[0]["requires_confirmation"] is True
    assert target_path == "Jan/Auto/Tesla"
    assert moved
    assert not orch.deferred_organizations
    assert config.final_dir.joinpath("Jan", "Auto", "Tesla", source_file.name).exists()
    assert "Jan/Auto/Tesla" in FolderRegistry(tmp_path).get_known_paths()
