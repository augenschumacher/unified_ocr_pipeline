from pathlib import Path

from core.config import AppConfig
from core.maintenance import cleanup_runtime_artifacts


def test_cleanup_runtime_artifacts_removes_only_selected_reserved_dirs(tmp_path):
    config = AppConfig(tmp_path)
    config.ensure_directories()

    work_file = config.work_dir / "page.png"
    work_subdir = config.work_dir / "work_job"
    error_file = config.error_dir / "failed.pdf"
    log_file = config.log_dir / "pipeline.log"
    original_file = config.original_dir / "input.pdf"
    final_file = config.final_dir / "output.pdf"

    work_file.write_text("work", encoding="utf-8")
    work_subdir.mkdir()
    (work_subdir / "ocr.txt").write_text("ocr", encoding="utf-8")
    error_file.write_text("error", encoding="utf-8")
    log_file.write_text("log", encoding="utf-8")
    original_file.write_text("original", encoding="utf-8")
    final_file.write_text("final", encoding="utf-8")

    audit = cleanup_runtime_artifacts(
        config,
        include_work=True,
        include_error=True,
        include_logs=False,
    )

    assert len(audit["deleted"]) == 3
    assert not work_file.exists()
    assert not work_subdir.exists()
    assert not error_file.exists()
    assert log_file.exists()
    assert original_file.exists()
    assert final_file.exists()
    assert config.work_dir.exists()
    assert config.error_dir.exists()


def test_cleanup_runtime_artifacts_dry_run_keeps_files(tmp_path):
    config = AppConfig(tmp_path)
    config.ensure_directories()
    artifact = config.work_dir / "keep.tmp"
    artifact.write_text("tmp", encoding="utf-8")

    audit = cleanup_runtime_artifacts(config, include_work=True, dry_run=True)

    assert audit["dry_run"] is True
    assert audit["deleted"][0]["dry_run"] is True
    assert artifact.exists()


def test_cleanup_runtime_artifacts_rejects_manipulated_runtime_path(tmp_path):
    config = AppConfig(tmp_path)
    config.ensure_directories()
    outside = tmp_path / "outside"
    outside.mkdir()
    config.work_dir = outside

    try:
        cleanup_runtime_artifacts(config, include_work=True)
    except ValueError as exc:
        assert "Unsicherer Cleanup-Zielpfad" in str(exc)
    else:
        raise AssertionError("unsafe cleanup target was not rejected")


def test_cleanup_removes_legacy_temp_work_only_when_requested(tmp_path):
    from core.config import AppConfig
    from core.maintenance import cleanup_runtime_artifacts

    config = AppConfig(tmp_path)
    config.ensure_directories()

    legacy = tmp_path / "temp_work" / "altes_dokument"
    legacy.mkdir(parents=True)
    (legacy / "page_0.png").write_bytes(b"bild")

    # Ohne das Flag bleibt der Altbestand unangetastet.
    cleanup_runtime_artifacts(config, include_work=True)
    assert legacy.exists()

    audit = cleanup_runtime_artifacts(
        config,
        include_work=False,
        include_legacy_temp_work=True,
    )

    assert not legacy.exists()
    assert (tmp_path / "temp_work").exists()
    assert any(entry["label"] == "temp_work" for entry in audit["deleted"])
