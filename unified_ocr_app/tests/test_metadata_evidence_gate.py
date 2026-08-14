from pathlib import Path
from unittest.mock import MagicMock

import pytest

from core.cloud.folder_registry import UnsafeArchivePath
from core.cloud.folder_registry import FolderRegistry
from core.config import AppConfig
from core.metadata import assess_metadata_evidence, normalize_metadata
from core.pipeline import PipelineOrchestrator


def _orchestrator(tmp_path: Path) -> PipelineOrchestrator:
    return PipelineOrchestrator(config=AppConfig(tmp_path), llm_client=MagicMock())


def test_empty_machine_description_enters_quality_review(tmp_path):
    metadata = normalize_metadata({}, source_text="Nur unstrukturierter Inhalt")
    evidence = assess_metadata_evidence(metadata, "Nur unstrukturierter Inhalt")
    report = {
        "severity": "info",
        "quality_status": "ok",
        "quality_score": 100,
        "warnings": [],
        "review_reasons": [],
        "review": {"required": False, "reasons": []},
    }

    _orchestrator(tmp_path)._merge_metadata_evidence_quality(report, evidence)

    assert report["quality_status"] == "review"
    assert report["requires_review"] is True
    assert report["metadata_evidence"]["requires_review"] is True
    assert any(
        reason["code"] == "metadata_description_empty"
        for reason in report["review_reasons"]
    )


def test_grounded_metadata_does_not_change_an_existing_clean_quality_gate(tmp_path):
    metadata = normalize_metadata(
        {"title": "Rechnung", "document_type": "Rechnung", "tags": ["Rechnung"]},
        source_text="Rechnung",
    )
    evidence = assess_metadata_evidence(metadata, "Rechnung")
    report = {
        "quality_status": "ok",
        "quality_score": 100,
        "requires_review": False,
        "review_required": False,
    }

    _orchestrator(tmp_path)._merge_metadata_evidence_quality(report, evidence)

    assert report["quality_status"] == "ok"
    assert report["requires_review"] is False
    assert report["metadata_evidence"]["status"] == "grounded"


def test_manual_target_path_strict_mode_never_falls_back_to_sonstiges(tmp_path):
    orchestrator = _orchestrator(tmp_path)
    outside = tmp_path.parent / "outside" / "Akte"

    with pytest.raises(UnsafeArchivePath, match="außerhalb"):
        orchestrator._normalize_target_path(str(outside), ["Fabio", "Sonstiges"], strict=True)

    with pytest.raises(UnsafeArchivePath, match="Unbekannte erste Ordnerebene"):
        orchestrator._normalize_target_path(
            "Unbekannt/Steuern",
            ["Fabio", "Sonstiges"],
            strict=True,
        )

    assert orchestrator._normalize_target_path(
        "Unbekannt/Steuern",
        ["Fabio", "Sonstiges"],
    ) == "Sonstiges/Steuern"


def test_manual_target_path_strict_mode_keeps_valid_canonical_person(tmp_path):
    orchestrator = _orchestrator(tmp_path)

    assert orchestrator._normalize_target_path(
        "fabio/Steuern/Bescheid",
        ["Fabio", "Sonstiges"],
        strict=True,
    ) == "Fabio/Steuern/Bescheid"


def test_invalid_manual_target_is_staged_for_review_instead_of_published(tmp_path):
    config = AppConfig(tmp_path)
    config.final_dir.mkdir(parents=True, exist_ok=True)
    registry = FolderRegistry(tmp_path)
    registry.add_person("Fabio")
    registry.add_person("Sonstiges")
    orchestrator = PipelineOrchestrator(config=config, llm_client=MagicMock())
    orchestrator._current_job_id = "manual-path-job"
    orchestrator._chosen_target_path = "Unbekannt/Steuern"
    artifact = config.final_dir / "akte.pdf"
    artifact.write_bytes(b"pdf evidence")

    moved, target = orchestrator._stage_organize(
        "Rechnung",
        {"title": "Rechnung"},
        "akte",
        artifact_paths={"pdf": artifact},
        quality_report={"quality_status": "ok", "requires_review": False},
    )

    assert target == "Sonstiges"
    assert orchestrator._current_organization_deferred is True
    assert artifact.exists() is False
    assert moved == [str(config.final_dir / "_staging" / "manual-path-job" / "akte.pdf")]
    assert (config.final_dir / "Sonstiges" / "akte.pdf").exists() is False
