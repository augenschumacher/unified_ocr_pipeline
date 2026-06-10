import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.config import AppConfig
from core.diagnostics import DiagnosticsRecorder, text_stats
from core.pipeline import PipelineOrchestrator


def test_text_stats_records_hash_and_preview_without_full_text():
    text = "Alpha Beta Gamma " * 20
    stats = text_stats(text, preview_chars=12)

    assert stats["chars"] == len(text)
    assert stats["words"] == 60
    assert stats["sha256"]
    assert stats["preview"] == text.strip()[:12]
    assert stats["preview"] != text


def test_diagnostics_recorder_redacts_secret_keys(tmp_path):
    source = tmp_path / "input.pdf"
    source.write_bytes(b"input")
    recorder = DiagnosticsRecorder(job_id="job-1", source_path=source)
    recorder.configure(api_key="secret", nested={"password": "pw", "safe": "ok"})
    recorder.stage("test", token_path="token.json", value=1)
    out = tmp_path / "debug.json"

    recorder.write_copy(out)
    data = json.loads(out.read_text(encoding="utf-8"))

    assert data["config"]["api_key"] == "<redacted>"
    assert data["config"]["nested"]["password"] == "<redacted>"
    assert data["config"]["nested"]["safe"] == "ok"
    assert data["stages"]["test"]["payload"]["token_path"] == "<redacted>"


@patch("core.pipeline.shutil.move")
def test_process_file_writes_debug_report(mock_move, tmp_path):
    config = AppConfig(tmp_path)
    config.original_dir.mkdir(parents=True, exist_ok=True)
    config.final_dir.mkdir(parents=True, exist_ok=True)

    llm = MagicMock()
    llm.vision_model = "Keins"
    llm.glm_ocr_model = "Keins"
    llm.fusion_model = "Keins"
    llm.analysis_model = "Keins"

    orch = PipelineOrchestrator(
        config=config,
        llm_client=llm,
        organize_enabled=False,
        save_docx_enabled=False,
        save_json_enabled=False,
        gdrive_enabled=False,
        debug_artifacts_enabled=True,
    )
    orch._stage_prepare = MagicMock(return_value=tmp_path / "work.pdf")
    orch._stage_ocrmypdf = MagicMock(return_value=(tmp_path / "ocr.pdf", "ocr text"))
    orch._stage_docling = MagicMock(return_value=("docling text", {}))
    orch._stage_extract_pages = MagicMock(return_value=([], {}))
    orch._stage_fusion = MagicMock(return_value={1: "fused text"})
    orch._stage_quality = MagicMock(return_value=("fused text", {"warnings": []}))
    orch._stage_analysis = MagicMock(return_value=({}, "final_name"))
    orch._stage_export = MagicMock(return_value={"pdf": None, "txt": None, "docx": None, "json": None})

    source = tmp_path / "input.pdf"
    source.write_text("input", encoding="utf-8")
    orch.process_file(source)

    debug_report = config.final_dir / "begleitdateien" / "final_name_debug_report.json"
    manifest = config.final_dir / "begleitdateien" / "final_name_job_manifest.json"
    assert debug_report.exists()
    data = json.loads(debug_report.read_text(encoding="utf-8"))
    assert data["stages"]["ocrmypdf"]["payload"]["text_chars"] == 8
    assert data["text_sources"]["ocr_sidecar"]["sha256"]
    manifest_data = json.loads(manifest.read_text(encoding="utf-8"))
    assert manifest_data["stages"]["diagnostics"]["status"] == "ok"
