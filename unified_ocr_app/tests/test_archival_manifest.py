import json

from core.config import AppConfig
from core.manifest import JobManifest


def test_manifest_records_final_output_fixity_and_effective_review(tmp_path):
    config = AppConfig(tmp_path)
    config.ensure_directories()
    source = tmp_path / "source.pdf"
    source.write_bytes(b"source")
    output = config.final_dir / "record.pdf"
    output.write_bytes(b"archival output")

    manifest = JobManifest.create(
        job_id="job-integrity",
        source_path=source,
        manifest_dir=config.work_dir / "job-integrity",
    )
    manifest.record_outputs({"pdf": output, "txt": None})
    manifest.record_review({"status": "confirmed", "text_changed": True})
    manifest.record_quality({"quality_status": "review", "requires_review": True})
    manifest.finalize("completed_with_warnings")

    data = json.loads(manifest.path.read_text(encoding="utf-8"))
    assert data["outputs"]["pdf"] == str(output)
    assert data["output_integrity"]["pdf"]["sha256"]
    assert data["output_integrity"]["pdf"]["size_bytes"] == len(b"archival output")
    assert data["output_integrity"]["txt"] is None
    assert data["review"]["text_changed"] is True
    assert data["quality"]["requires_review"] is True
    assert data["finalized_at"]


def test_error_cleanup_requires_explicit_retention_limit(tmp_path):
    config = AppConfig(tmp_path)
    config.ensure_directories()
    old_error = config.error_dir / "old-failure.pdf"
    old_error.write_bytes(b"irreplaceable input")

    assert config.cleanup_error_dir() == []
    assert old_error.exists()
