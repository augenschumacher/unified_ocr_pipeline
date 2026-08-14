import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from core.cloud.classification_memory import ClassificationMemory
from core.cache import sha256_file
from core.cloud.folder_registry import FolderRegistry
from core.config import AppConfig
from core.local_store import LocalStore
from core.review_service import ReviewQueueService, ReviewResolutionError
from core.pipeline import PipelineOrchestrator


def _staged_review(tmp_path: Path, *, kind: str = "ocr_quality"):
    config = AppConfig(str(tmp_path))
    config.ensure_directories()
    registry = FolderRegistry(config.base_dir)
    registry.add_person("Jan")
    registry.add_person("Sonstiges")
    registry.add_path("Jan/Finanzen/Rechnungen")

    original = config.original_dir / "scan.pdf"
    original.write_bytes(b"original")
    staging = config.final_dir / "_staging" / "job-1"
    staging.mkdir(parents=True)
    pdf = staging / "2026_scan.pdf"
    text = staging / "2026_scan.txt"
    report = staging / "2026_scan_quality_report.json"
    manifest = staging / "2026_scan_job_manifest.json"
    fitz = pytest.importorskip("fitz")
    document = fitz.open()
    document.new_page().insert_text((72, 72), "Rechnung 123")
    document.save(pdf)
    document.close()
    text.write_text("Rechnung 123", encoding="utf-8")
    report.write_text("{}", encoding="utf-8")
    manifest.write_text(
        json.dumps(
            {
                "schema": "unified_ocr_job_manifest_v1",
                "job_id": "job-1",
                "status": "review_required",
                "outputs": {"pdf": str(pdf), "txt": str(text), "json": str(report)},
            }
        ),
        encoding="utf-8",
    )

    store = LocalStore(config)
    store.start_job("job-1", original, "source-hash")
    store.update_job(
        "job-1",
        "review_required",
        final_name="2026_scan",
        metadata={"document_type": "Rechnung", "tags": ["rechnung"]},
    )
    item_id = store.add_review_item(
        job_id="job-1",
        kind=kind,
        status="staged",
        source_name="2026_scan",
        proposed_path="Jan/Finanzen/Rechnungen",
        metadata={"document_type": "Rechnung", "tags": ["rechnung"]},
        payload={
            "fused_text": "Rechnung 123",
            "classification": {
                "recommended_path": "Jan/Finanzen/Rechnungen",
                "candidates": [{"path": "Jan/Finanzen/Rechnungen", "score": 90}],
            },
            "staging_dir": str(staging),
        },
        quality={
            "quality_status": "review",
            "quality_score": 72,
            "requires_review": True,
            "warnings": ["Betrag prüfen"],
        },
        artifacts={
            "pdf": str(pdf),
            "txt": str(text),
            "quality": str(report),
            "job_manifest": str(manifest),
        },
    )
    return config, store, item_id, (pdf, text, report, manifest)


def test_ocr_quality_review_cannot_be_resolved_by_folder_choice_alone(tmp_path):
    config, store, item_id, artifacts = _staged_review(tmp_path)

    with pytest.raises(ReviewResolutionError, match="ausdrücklich bestätigt"):
        ReviewQueueService(config).resolve(
            item_id,
            "Jan/Finanzen/Rechnungen",
            quality_confirmed=False,
        )

    assert store.get_review_item(item_id)["status"] == "staged"
    assert all(path.is_file() for path in artifacts)
    assert not (config.final_dir / "Jan" / "Finanzen" / "Rechnungen" / artifacts[0].name).exists()


def test_confirmed_review_moves_whole_package_then_resolves_and_learns(tmp_path):
    config, store, item_id, artifacts = _staged_review(tmp_path)
    service = ReviewQueueService(config)

    result = service.resolve(
        item_id,
        "Jan/Finanzen/Rechnungen",
        quality_confirmed=True,
        review_note="Betrag und Datum mit dem Original verglichen.",
    )

    target = config.final_dir / "Jan" / "Finanzen" / "Rechnungen"
    assert result["target_path"] == "Jan/Finanzen/Rechnungen"
    assert all((target / path.name).is_file() for path in artifacts)
    assert not any(path.exists() for path in artifacts)

    resolved = store.get_review_item(item_id)
    assert resolved["status"] == "resolved"
    assert resolved["chosen_path"] == "Jan/Finanzen/Rechnungen"
    assert resolved["payload"]["resolution"]["quality_confirmed"] is True
    assert store.get_job("job-1")["status"] == "completed_after_review"
    assert store.search_documents("2026_scan")[0]["target_path"] == "Jan/Finanzen/Rechnungen"

    manifest = json.loads((target / artifacts[-1].name).read_text(encoding="utf-8"))
    assert manifest["status"] == "completed_after_review"
    assert manifest["review"]["chosen_path"] == "Jan/Finanzen/Rechnungen"
    assert manifest["outputs"]["pdf"] == str(target / artifacts[0].name)

    decisions = ClassificationMemory(config.base_dir).data["decisions"]
    assert decisions
    assert decisions[-1]["chosen_path"] == "Jan/Finanzen/Rechnungen"


def test_review_reconciles_pdf_postflight_in_sidecar_and_manifest(tmp_path):
    config, store, item_id, artifacts = _staged_review(tmp_path)
    item = store.get_review_item(item_id)
    quality = dict(item["quality"])
    quality["pdf_postflight"] = {"ok": True, "path": str(artifacts[0])}
    artifacts[2].write_text(json.dumps(quality), encoding="utf-8")
    store.update_review_item(item_id, status="staged", quality=quality)

    ReviewQueueService(config).resolve(
        item_id,
        "Jan/Finanzen/Rechnungen",
        quality_confirmed=True,
    )

    target = config.final_dir / "Jan" / "Finanzen" / "Rechnungen"
    final_pdf = target / artifacts[0].name
    sidecar = json.loads((target / artifacts[2].name).read_text(encoding="utf-8"))
    manifest = json.loads((target / artifacts[3].name).read_text(encoding="utf-8"))
    assert sidecar["pdf_postflight"]["path"] == str(final_pdf)
    assert manifest["quality"]["pdf_postflight"]["path"] == str(final_pdf)


def test_package_move_failure_keeps_review_recoverable_and_sources_intact(tmp_path, monkeypatch):
    config, store, item_id, artifacts = _staged_review(tmp_path, kind="sorting_uncertain")
    service = ReviewQueueService(config)

    def fail_move(*_args, **_kwargs):
        raise OSError("simulierter Datenträgerfehler")

    monkeypatch.setattr(service.organizer, "organize_artifacts", fail_move)
    with pytest.raises(ReviewResolutionError, match="Datenträgerfehler"):
        service.resolve(
            item_id,
            "Jan/Finanzen/Rechnungen",
            quality_confirmed=True,
        )

    failed = store.get_review_item(item_id)
    assert failed["status"] == "failed"
    assert "Datenträgerfehler" in failed["error"]
    assert all(path.is_file() for path in artifacts)
    assert not ClassificationMemory(config.base_dir).data["decisions"]


def test_reviewed_text_and_metadata_are_persisted_before_publication(tmp_path):
    config, store, item_id, artifacts = _staged_review(tmp_path)
    corrected_text = "Geprüfte Rechnung RE-123 mit Betrag 42,00 EUR"

    ReviewQueueService(config).resolve(
        item_id,
        "Jan/Finanzen/Rechnungen",
        quality_confirmed=True,
        corrected_text=corrected_text,
        corrected_metadata={
            "document_date": "2026-07-12",
            "title": "Geprüfte Rechnung",
            "document_type": "Invoice",
            "tags": ["Invoice", "Dokument"],
        },
    )

    target = config.final_dir / "Jan" / "Finanzen" / "Rechnungen"
    assert (target / artifacts[1].name).read_text(encoding="utf-8") == corrected_text
    quality = json.loads((target / artifacts[2].name).read_text(encoding="utf-8"))
    assert quality["human_review"]["text_changed"] is True
    assert quality["human_review"]["metadata_changed"] is True
    assert quality["reviewed_metadata"]["tags"] == ["Rechnung"]

    resolved = store.get_review_item(item_id)
    assert resolved["metadata"]["document_date"] == "2026-07-12"
    assert resolved["metadata"]["title"] == "Geprüfte Rechnung"
    manifest = json.loads((target / artifacts[3].name).read_text(encoding="utf-8"))
    assert manifest["metadata"]["title"] == "Geprüfte Rechnung"
    pikepdf = pytest.importorskip("pikepdf")
    with pikepdf.open(target / artifacts[0].name) as pdf_document:
        assert str(pdf_document.docinfo.get("/Title")) == "Geprüfte Rechnung"
        with pdf_document.open_metadata() as xmp:
            assert str(xmp.get("dc:title")) == "Geprüfte Rechnung"
            assert str(xmp.get("pdf:Keywords")) == "Rechnung"


def test_empty_reviewed_text_is_rejected_without_publication(tmp_path):
    config, store, item_id, artifacts = _staged_review(tmp_path)

    with pytest.raises(ReviewResolutionError, match="nicht.*geleert"):
        ReviewQueueService(config).resolve(
            item_id,
            "Jan/Finanzen/Rechnungen",
            quality_confirmed=True,
            corrected_text="   \n",
        )

    assert store.get_review_item(item_id)["status"] == "failed"
    assert artifacts[1].read_text(encoding="utf-8") == "Rechnung 123"


def test_corrections_and_roles_survive_publication_failure(tmp_path, monkeypatch):
    config, store, item_id, artifacts = _staged_review(tmp_path)
    service = ReviewQueueService(config)

    def fail_move(*_args, **_kwargs):
        raise OSError("Publikation gestoppt")

    monkeypatch.setattr(service.organizer, "organize_artifacts", fail_move)
    with pytest.raises(ReviewResolutionError, match="Publikation gestoppt"):
        service.resolve(
            item_id,
            "Jan/Finanzen/Rechnungen",
            quality_confirmed=True,
            corrected_text="Geprüfter Text RE-999",
            corrected_metadata={"title": "Geprüfter Titel", "document_type": "Rechnung"},
        )

    failed = store.get_review_item(item_id)
    assert failed["status"] == "failed"
    assert failed["payload"]["fused_text"] == "Geprüfter Text RE-999"
    assert failed["metadata"]["title"] == "Geprüfter Titel"
    assert set(failed["artifacts"]) == {"pdf", "txt", "quality", "job_manifest"}
    assert artifacts[1].read_text(encoding="utf-8") == "Geprüfter Text RE-999"


def test_legacy_docx_is_never_overwritten_by_text_review(tmp_path):
    config, store, item_id, artifacts = _staged_review(tmp_path, kind="sorting_uncertain")
    docx_path = artifacts[0].parent / "source.docx"
    original_bytes = b"born-digital-docx-evidence"
    docx_path.write_bytes(original_bytes)
    item = store.get_review_item(item_id)
    store.update_review_item(
        item_id,
        artifacts={**item["artifacts"], "docx": str(docx_path)},
        payload={**item["payload"]},  # deliberately no is_docx provenance
    )

    result = ReviewQueueService(config).resolve(
        item_id,
        "Jan/Finanzen/Rechnungen",
        quality_confirmed=True,
        corrected_text="Geprüfter Begleittext",
    )

    assert Path(result["artifacts"]["docx"]).read_bytes() == original_bytes
    assert "reviewed_docx" not in result["artifacts"]


def test_generated_proof_docx_is_preserved_and_review_copy_is_added(tmp_path):
    config, store, item_id, artifacts = _staged_review(tmp_path, kind="sorting_uncertain")
    proof_docx = artifacts[0].parent / "machine_proof.docx"
    proof_bytes = b"image-bearing-machine-proof-must-remain-immutable"
    proof_docx.write_bytes(proof_bytes)
    row = store.get_review_item(item_id)
    store.update_review_item(
        item_id,
        artifacts={**row["artifacts"], "docx": str(proof_docx)},
        payload={
            **row["payload"],
            "is_docx": False,
            "docx_mode": "Prüf-DOCX",
            "original_path": str(config.original_dir / "scan.pdf"),
        },
    )

    result = ReviewQueueService(config).resolve(
        item_id,
        "Jan/Finanzen/Rechnungen",
        quality_confirmed=True,
        corrected_text="Geprüfter Text für die lesbare Review-Ableitung",
    )

    assert Path(result["artifacts"]["docx"]).read_bytes() == proof_bytes
    reviewed_docx = Path(result["artifacts"]["reviewed_docx"])
    assert reviewed_docx.is_file()
    assert reviewed_docx.name.endswith("_reviewed.docx")


def test_prepared_move_intent_recovers_artifacts_after_crash_window(tmp_path):
    config, store, item_id, artifacts = _staged_review(tmp_path, kind="sorting_uncertain")
    staged_target = config.final_dir / "_staging" / "recovered-job"
    staged_target.mkdir(parents=True)
    moved = []
    intent_artifacts = {}
    for role, source in zip(("pdf", "txt", "quality", "job_manifest"), artifacts):
        digest = sha256_file(source)
        destination = staged_target / source.name
        source.replace(destination)
        moved.append(destination)
        intent_artifacts[role] = {
            "source": str(source),
            "name": source.name,
            "sha256": digest,
        }
    row = store.get_review_item(item_id)
    store.update_review_item(
        item_id,
        artifacts={role: str(path) for role, path in zip(intent_artifacts, artifacts)},
        payload={
            **row["payload"],
            "staging_dir": str(staged_target),
            "move_intent": {
                "phase": "prepared",
                "target_dir": str(staged_target),
                "target_label": "_staging/recovered-job",
                "artifacts": intent_artifacts,
            },
        },
    )

    result = ReviewQueueService(config).resolve(
        item_id,
        "Jan/Finanzen/Rechnungen",
        quality_confirmed=True,
    )

    assert set(result["artifacts"]) == {"pdf", "txt", "quality", "job_manifest"}
    assert all(Path(path).is_file() for path in result["artifacts"].values())


def test_prepared_evidence_move_intent_recovers_manifest_after_crash_window(tmp_path):
    config, store, item_id, artifacts = _staged_review(tmp_path, kind="sorting_uncertain")
    manifest = artifacts[-1]
    evidence_target = manifest.parent / "evidence-committed"
    evidence_target.mkdir()
    digest = sha256_file(manifest)
    moved_manifest = evidence_target / manifest.name
    manifest.replace(moved_manifest)
    row = store.get_review_item(item_id)
    store.update_review_item(
        item_id,
        artifacts={**row["artifacts"], "job_manifest": str(manifest)},
        payload={
            **row["payload"],
            "evidence_move_intent": {
                "phase": "prepared",
                "target_dir": str(evidence_target),
                "target_label": "_staging/job-1/evidence",
                "artifacts": {
                    "job_manifest": {
                        "source": str(manifest),
                        "name": manifest.name,
                        "sha256": digest,
                    }
                },
            },
        },
    )

    result = ReviewQueueService(config).resolve(
        item_id,
        "Jan/Finanzen/Rechnungen",
        quality_confirmed=True,
    )

    assert Path(result["artifacts"]["job_manifest"]).is_file()
    assert store.get_review_item(item_id)["status"] == "resolved"


def test_atomic_review_claim_allows_only_one_parallel_resolver(tmp_path, monkeypatch):
    config, store, item_id, _artifacts = _staged_review(tmp_path, kind="sorting_uncertain")
    import threading

    barrier = threading.Barrier(2)
    original_claim = LocalStore.claim_review_item

    def synchronized_claim(self, *args, **kwargs):
        barrier.wait(timeout=5)
        return original_claim(self, *args, **kwargs)

    monkeypatch.setattr(LocalStore, "claim_review_item", synchronized_claim)

    def resolve_once():
        try:
            ReviewQueueService(config).resolve(
                item_id,
                "Jan/Finanzen/Rechnungen",
                quality_confirmed=True,
            )
            return "resolved"
        except ReviewResolutionError:
            return "rejected"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _index: resolve_once(), range(2)))

    assert sorted(outcomes) == ["rejected", "resolved"]
    assert store.get_review_item(item_id)["status"] == "resolved"


def test_review_claim_requires_owner_token_and_only_expires_by_lease(tmp_path):
    _config, store, item_id, _artifacts = _staged_review(tmp_path, kind="sorting_uncertain")
    initial = store.get_review_item(item_id)
    claimed = store.claim_review_item(item_id, expected_revision=initial["revision"])

    assert claimed is not None
    token = claimed["claim_token"]
    assert token
    assert claimed["claim_expires_at"] > claimed["updated_at"]
    with pytest.raises(PermissionError, match="aktiven Claim"):
        store.update_review_item(item_id, status="in_review", metadata={"title": "ohne Token"})
    with pytest.raises(PermissionError, match="aktiv bearbeitet"):
        store.open_review_item(item_id)
    with pytest.raises(PermissionError, match="aktiver Review-Claim"):
        store.resume_review_item(item_id)
    with pytest.raises(PermissionError, match="aktiven Claim"):
        store.update_review_item(
            item_id,
            status="in_review",
            claim_token="falscher-token",
            metadata={"title": "fremd"},
        )
    assert store.claim_review_item(item_id, expected_revision=claimed["revision"]) is None

    expired = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    with sqlite3.connect(store.db_path) as connection:
        connection.execute(
            "UPDATE review_queue SET claim_expires_at = ? WHERE id = ?",
            (expired, item_id),
        )
        connection.commit()
    reclaimed = store.claim_review_item(item_id, expected_revision=claimed["revision"])
    assert reclaimed is not None
    assert reclaimed["claim_token"] != token
    with pytest.raises(PermissionError, match="aktiven Claim|zwischenzeitlich"):
        store.update_review_item(
            item_id,
            status="in_review",
            claim_token=token,
            expected_revision=claimed["revision"],
            metadata={"title": "veralteter Bearbeiter"},
        )
    assert store.get_review_item(item_id)["metadata"].get("title") != "veralteter Bearbeiter"


def test_review_lookup_by_job_id_is_not_limited_by_queue_backlog(tmp_path):
    config = AppConfig(str(tmp_path))
    config.ensure_directories()
    store = LocalStore(config)
    now = datetime.now(timezone.utc).isoformat()
    with store._connect() as connection:
        connection.executemany(
            """
            INSERT INTO review_queue (
                job_id, kind, status, source_name, proposed_path,
                candidates_json, metadata_json, payload_json,
                artifacts_json, quality_json, error, created_at, updated_at
            ) VALUES (?, 'sorting_uncertain', 'staged', ?, 'Sonstiges',
                      '[]', '{}', '{}', '{}', '{}', '', ?, ?)
            """,
            [
                (f"backlog-{index}", f"backlog-{index}.pdf", now, now)
                for index in range(1001)
            ],
        )
    wanted_id = store.add_review_item(
        job_id="wanted-job",
        kind="sorting_uncertain",
        status="staged",
        source_name="wanted.pdf",
        proposed_path="Sonstiges",
    )

    assert all(
        row["job_id"] != "wanted-job"
        for row in store.list_recoverable_work(limit=1000)
    )
    found = store.get_review_by_job_id("wanted-job")
    assert found is not None
    assert found["id"] == wanted_id


def test_confirmed_sorting_waits_for_manifest_and_sync_before_resolution(tmp_path):
    config = AppConfig(str(tmp_path))
    config.ensure_directories()
    registry = FolderRegistry(config.base_dir)
    registry.add_person("Jan")
    registry.add_person("Sonstiges")
    registry.add_path("Jan/Finanzen/Rechnungen")
    source = config.original_dir / "direct-confirm.pdf"
    source.write_bytes(b"source")
    store = LocalStore(config)
    store.start_job("direct-confirm-job", source, "direct-confirm-hash")

    class _PromptedClassifier:
        def run_classification(self, *_args, **_kwargs):
            return {
                "recommended_path": "Jan/Finanzen/Rechnungen",
                "confidence": 45,
                "review_required": True,
                "candidates": [],
            }

    published_pdf = config.final_dir / "direct-confirm.pdf"
    published_pdf.write_bytes(b"pdf")
    orchestrator = PipelineOrchestrator(
        config,
        _PromptedClassifier(),
        prompt_sorting_callback=lambda *_args: "Jan/Finanzen/Rechnungen",
    )
    orchestrator._current_job_id = "direct-confirm-job"
    orchestrator._current_manifest_required = True
    staged, target_path = orchestrator._stage_organize(
        "Rechnung 123",
        {"document_type": "Rechnung"},
        "direct-confirm",
        artifact_paths={"pdf": published_pdf},
    )

    assert target_path == "Jan/Finanzen/Rechnungen"
    assert len(orchestrator.deferred_organizations) == 1
    assert all("_staging" in str(path) for path in staged)
    pending = store.get_review_by_job_id("direct-confirm-job")
    assert pending is not None
    assert pending["status"] == "staged"
    assert not config.final_dir.joinpath("Jan", "Finanzen", "Rechnungen", "direct-confirm.pdf").exists()

    manifest = Path(staged[0]).parent / "direct-confirm_job_manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema": "unified_ocr_job_manifest_v1",
                "job_id": "direct-confirm-job",
                "status": "review_required",
                "outputs": {"pdf": str(staged[0])},
            }
        ),
        encoding="utf-8",
    )
    store.update_review_item(
        pending["id"],
        expected_revision=pending["revision"],
        status="staged",
        artifacts={**pending["artifacts"], "job_manifest": str(manifest)},
    )

    callback_states = []

    def interrupted_sync(context, *, is_docx):
        callback_states.append(store.get_review_item(pending["id"])["status"])
        callback_manifest = Path(context["artifacts"]["job_manifest"])
        callback_states.append(json.loads(callback_manifest.read_text(encoding="utf-8"))["status"])
        raise RuntimeError("simulierter Verbindungsabbruch")

    orchestrator.gdrive_enabled = True
    orchestrator._sync_published_review_package = interrupted_sync
    orchestrator.process_deferred_organizations()

    failed = store.get_review_item(pending["id"])
    assert callback_states == ["in_review", "review_sync_pending"]
    assert failed["status"] == "failed"
    assert store.get_job("direct-confirm-job")["status"] != "completed_after_review"
    assert store.search_documents() == []
    final_manifest = Path(failed["artifacts"]["job_manifest"])
    assert json.loads(final_manifest.read_text(encoding="utf-8"))["status"] == "sync_failed"


def test_modern_review_without_manifest_stays_recoverable(tmp_path):
    config, store, item_id, artifacts = _staged_review(
        tmp_path,
        kind="sorting_uncertain",
    )
    artifacts[-1].unlink()
    row = store.get_review_item(item_id)
    store.update_review_item(
        item_id,
        expected_revision=row["revision"],
        payload={**row["payload"], "manifest_required": True},
        artifacts={role: path for role, path in row["artifacts"].items() if role != "job_manifest"},
    )

    with pytest.raises(ReviewResolutionError, match="Archivmanifest"):
        ReviewQueueService(config).resolve(
            item_id,
            "Jan/Finanzen/Rechnungen",
            quality_confirmed=True,
        )

    failed = store.get_review_item(item_id)
    assert failed["status"] == "failed"
    assert artifacts[0].is_file()
    assert not config.final_dir.joinpath("Jan", "Finanzen", "Rechnungen", artifacts[0].name).exists()


def test_remote_sync_failure_happens_before_final_resolve_and_stays_recoverable(tmp_path):
    config, store, item_id, artifacts = _staged_review(tmp_path, kind="sorting_uncertain")
    callback_states = []

    def fail_sync(context):
        callback_states.append(store.get_review_item(item_id)["status"])
        manifest_path = Path(context["artifacts"]["job_manifest"])
        callback_states.append(json.loads(manifest_path.read_text(encoding="utf-8"))["status"])
        return [{"provider": "test", "action": "failed", "error": "offline"}]

    with pytest.raises(ReviewResolutionError, match="Remote-Synchronisierung"):
        ReviewQueueService(config).resolve(
            item_id,
            "Jan/Finanzen/Rechnungen",
            quality_confirmed=True,
            post_publish_callback=fail_sync,
        )

    assert callback_states == ["in_review", "review_sync_pending"]
    failed = store.get_review_item(item_id)
    assert failed["status"] == "failed"
    assert failed["payload"]["sync_retry_required"] is True
    assert store.get_job("job-1")["status"] != "completed_after_review"
    assert store.search_documents("2026_scan") == []
    manifest = json.loads(Path(failed["artifacts"]["job_manifest"]).read_text(encoding="utf-8"))
    assert manifest["status"] == "sync_failed"
    assert manifest["sync"]["review"]["uploads"][0]["error"] == "offline"
    assert not any(path.exists() for path in artifacts)


def test_atomic_review_finalization_is_idempotent_and_upserts_document_once(tmp_path):
    _config, store, item_id, _artifacts = _staged_review(tmp_path, kind="sorting_uncertain")
    row = store.get_review_item(item_id)
    claimed = store.claim_review_item(item_id, expected_revision=row["revision"])
    token = claimed["claim_token"]
    fields = {
        "claim_token": token,
        "chosen_path": "Jan/Finanzen/Rechnungen",
        "target_path": "Jan/Finanzen/Rechnungen",
        "artifacts": {"pdf": "final/Jan/Finanzen/Rechnungen/2026_scan.pdf"},
        "metadata": {"document_type": "Rechnung"},
        "payload": {"resolution": {"status": "confirmed"}},
        "quality": {"quality_status": "ok"},
    }

    first = store.finalize_review_transaction(item_id, **fields)
    second = store.finalize_review_transaction(item_id, **fields)

    assert first["status"] == second["status"] == "resolved"
    assert store.get_job("job-1")["status"] == "completed_after_review"
    assert len(store.find_duplicates("source-hash", limit=20)) == 1
    finalized_events = [
        event for event in store.list_job_events("job-1") if event["event"] == "review_finalized"
    ]
    assert len(finalized_events) == 1


def test_schema_v5_deduplicates_legacy_hashes_and_enforces_partial_uniqueness(tmp_path):
    store = LocalStore(tmp_path)
    with sqlite3.connect(store.db_path) as connection:
        connection.execute("DROP INDEX uq_documents_source_sha")
        for title, updated_at in (("alt", "2026-01-01T00:00:00+00:00"), ("neu", "2026-02-01T00:00:00+00:00")):
            connection.execute(
                """
                INSERT INTO documents (
                    source_sha256, source_name, final_name, target_path,
                    outputs_json, metadata_json, created_at, updated_at
                ) VALUES ('legacy-hash', 'scan.pdf', ?, 'Jan/Akte', '{}', ?, ?, ?)
                """,
                (title, json.dumps({"title": title}), updated_at, updated_at),
            )
        connection.commit()

    migrated = LocalStore(tmp_path)
    matches = migrated.find_duplicates("legacy-hash", limit=20)
    assert len(matches) == 1
    assert matches[0]["final_name"] == "neu"
    with sqlite3.connect(migrated.db_path) as connection, pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            INSERT INTO documents (
                source_sha256, source_name, final_name, target_path,
                outputs_json, metadata_json, created_at, updated_at
            ) VALUES ('legacy-hash', 'x', 'x', 'x', '{}', '{}', '2026', '2026')
            """
        )


def test_successful_remote_sync_is_in_manifest_before_atomic_resolve(tmp_path):
    config, store, item_id, _artifacts = _staged_review(tmp_path, kind="sorting_uncertain")

    result = ReviewQueueService(config).resolve(
        item_id,
        "Jan/Finanzen/Rechnungen",
        quality_confirmed=True,
        post_publish_callback=lambda _context: [
            {"provider": "test", "action": "created", "remote_path": "Jan/Rechnung.pdf"}
        ],
    )

    assert result["item"]["status"] == "resolved"
    assert result["sync_completed"] is True
    manifest = json.loads(Path(result["artifacts"]["job_manifest"]).read_text(encoding="utf-8"))
    assert manifest["status"] == "completed_after_review"
    assert manifest["sync"]["review"]["phase"] == "committed"
    assert manifest["sync"]["review"]["uploads"][0]["action"] == "created"


def test_malformed_manifest_keeps_review_recoverable(tmp_path):
    config, store, item_id, artifacts = _staged_review(tmp_path, kind="sorting_uncertain")
    artifacts[-1].write_text("{broken", encoding="utf-8")

    with pytest.raises(ReviewResolutionError, match="Archivmanifest"):
        ReviewQueueService(config).resolve(
            item_id,
            "Jan/Finanzen/Rechnungen",
            quality_confirmed=True,
        )

    failed = store.get_review_item(item_id)
    assert failed["status"] == "failed"
    assert all(Path(path).is_file() for path in failed["artifacts"].values())


class _CertainClassifier:
    analysis_model = "Keins"

    def run_classification(self, *_args, **_kwargs):
        return {
            "recommended_path": "Jan/Finanzen/Rechnungen",
            "confidence": 96,
            "score": 96,
            "auto_assign": True,
            "review_required": False,
            "abstained": False,
            "candidates": [{"path": "Jan/Finanzen/Rechnungen", "score": 96}],
        }


def test_quality_gate_stages_even_a_certain_folder_classification(tmp_path):
    config = AppConfig(str(tmp_path))
    config.ensure_directories()
    registry = FolderRegistry(config.base_dir)
    registry.add_person("Jan")
    registry.add_path("Jan/Finanzen/Rechnungen")
    pdf = config.final_dir / "scan.pdf"
    txt = config.final_dir / "scan.txt"
    pdf.write_bytes(b"pdf")
    txt.write_text("Inhalt", encoding="utf-8")

    orchestrator = PipelineOrchestrator(config, _CertainClassifier())
    orchestrator._current_job_id = "quality-job"
    original = config.original_dir / "scan-original.pdf"
    original.write_bytes(b"original")
    orchestrator._current_original_path = original
    moved, target = orchestrator._stage_organize(
        "Inhalt",
        {"document_type": "Rechnung", "tags": ["rechnung"]},
        "scan",
        artifact_paths={"pdf": pdf, "txt": txt},
        quality_report={
            "quality_status": "review",
            "quality_score": 74,
            "requires_review": True,
            "review": {"required": True, "blocking": False},
            "warnings": ["Datum prüfen"],
        },
    )

    assert target == "Jan/Finanzen/Rechnungen"
    assert orchestrator._current_organization_deferred is True
    assert all("_staging" in str(path) for path in moved)
    assert not (config.final_dir / "Jan" / "Finanzen" / "Rechnungen" / "scan.pdf").exists()
    review = LocalStore(config).list_recoverable_work()[0]
    assert review["kind"] == "ocr_quality"
    assert review["status"] == "staged"
    assert review["payload"]["original_path"] == str(original)
    assert ReviewQueueService(config).original_path(review) == original


def test_explicit_manual_review_releases_quality_gate(tmp_path):
    config = AppConfig(str(tmp_path))
    config.ensure_directories()
    registry = FolderRegistry(config.base_dir)
    registry.add_person("Jan")
    registry.add_path("Jan/Finanzen/Rechnungen")
    pdf = config.final_dir / "scan.pdf"
    pdf.write_bytes(b"pdf")

    orchestrator = PipelineOrchestrator(config, _CertainClassifier())
    orchestrator._current_job_id = "reviewed-job"
    orchestrator._manual_review_completed = True
    orchestrator._chosen_target_path = "Jan/Finanzen/Rechnungen"
    moved, target = orchestrator._stage_organize(
        "Geprüfter Inhalt",
        {"document_type": "Rechnung"},
        "scan",
        artifact_paths={"pdf": pdf},
        quality_report={
            "quality_status": "review",
            "requires_review": True,
            "review": {"required": True},
        },
    )

    assert target == "Jan/Finanzen/Rechnungen"
    assert orchestrator._current_organization_deferred is False
    assert moved == [config.final_dir / "Jan" / "Finanzen" / "Rechnungen" / "scan.pdf"]
    assert LocalStore(config).list_recoverable_work() == []


def test_quality_gate_remains_active_when_folder_organization_is_disabled(tmp_path):
    config = AppConfig(str(tmp_path))
    config.ensure_directories()
    pdf = config.final_dir / "unsortiert.pdf"
    report = config.final_dir / "unsortiert_quality_report.json"
    pdf.write_bytes(b"pdf")
    report.write_text("{}", encoding="utf-8")
    orchestrator = PipelineOrchestrator(
        config,
        _CertainClassifier(),
        organize_enabled=False,
    )
    orchestrator._current_job_id = "unsorted-job"
    original = config.original_dir / "unsortiert-original.pdf"
    original.write_bytes(b"original")
    LocalStore(config).start_job("unsorted-job", original, "unsorted-hash")

    staged = orchestrator._stage_unsorted_quality_review(
        fused_text="Unsicherer Inhalt",
        metadata={"document_type": "Rechnung"},
        final_name="unsortiert",
        artifact_paths={"pdf": pdf, "quality": report},
        quality_report={
            "quality_status": "review",
            "requires_review": True,
            "review": {"required": True},
            "warnings": ["Wert prüfen"],
        },
        preview_pdf_path=pdf,
        is_docx=False,
    )

    assert all("_staging" in str(path) for path in staged)
    item = LocalStore(config).list_recoverable_work()[0]
    assert item["payload"]["organize_enabled"] is False

    result = ReviewQueueService(config).resolve(
        item["id"],
        "",
        quality_confirmed=True,
    )

    assert result["target_path"] == ""
    assert (config.final_dir / "unsortiert.pdf").is_file()
    assert (config.final_dir / "unsortiert_quality_report.json").is_file()
    assert LocalStore(config).get_review_item(item["id"])["chosen_path"] == "__archive_root__"
    assert ClassificationMemory(config.base_dir).data["decisions"] == []
