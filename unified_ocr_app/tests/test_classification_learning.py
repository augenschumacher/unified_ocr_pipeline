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
        "text",
        {},
        ["Fabio/Auto/Golf/Service"],
        MockLLM(),
        ["Fabio"],
        memory_candidates=[{
            "path": "Fabio/Auto/Golf/Service",
            "score": 92,
            "reason": "memory",
            "evidence": ["golf 7", "inspektion"],
        }],
    )

    assert res["recommended_path"] == "Fabio/Auto/Golf/Service"
    assert res["reason"] == "memory"
    assert res["confidence"] == 92


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
    )

    assert target_path == "Jan/Auto/Tesla"
    assert prompt.called
    assert moved
    assert config.final_dir.joinpath("Jan", "Auto", "Tesla", source_file.name).exists()

    data = json.loads(tmp_path.joinpath("classification_memory.json").read_text(encoding="utf-8"))
    assert data["decisions"][-1]["chosen_path"] == "Jan/Auto/Tesla"
    assert data["decisions"][-1]["source"] == "sorting_prompt"
