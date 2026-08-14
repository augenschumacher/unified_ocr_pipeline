import tempfile
from pathlib import Path
from unittest.mock import MagicMock

from core.cache import CacheInput, build_cache_key, sha256_text
from core.config import AppConfig
from core.pipeline import PipelineOrchestrator


def test_vision_cache_key_includes_image_hash():
    base = {
        "task": "vision_review",
        "model": "gemini/gemini-2.5-flash",
        "prompt_version": "1",
        "system_prompt_hash": sha256_text("system"),
        "user_prompt_hash": sha256_text("user"),
        "source_hashes": {"page_markdown": sha256_text("same markdown")},
        "options": {"page_num": 1},
    }

    key_a = build_cache_key(CacheInput(**base, image_sha256="a" * 64))
    key_b = build_cache_key(CacheInput(**base, image_sha256="b" * 64))

    assert key_a != key_b


def test_fusion_cache_key_includes_vision_source():
    common_sources = {
        "ocr_text": sha256_text("same ocr"),
        "glm_ocr_text": sha256_text("same glm"),
        "previous_page_text": sha256_text("same previous"),
    }
    base = {
        "task": "page_fusion",
        "model": "ollama/qwen",
        "prompt_version": "1",
        "system_prompt_hash": sha256_text("system"),
        "user_prompt_hash": sha256_text("user"),
        "options": {"page_num": 1, "is_tabular": False},
    }

    key_a = build_cache_key(CacheInput(**base, source_hashes={**common_sources, "vision_markdown": sha256_text("vision a")}))
    key_b = build_cache_key(CacheInput(**base, source_hashes={**common_sources, "vision_markdown": sha256_text("vision b")}))

    assert key_a != key_b


def test_stage_fusion_degraded_fallback_prevents_empty_output():
    with tempfile.TemporaryDirectory() as tmpdir:
        config = AppConfig(Path(tmpdir))
        llm = MagicMock()
        llm.fusion_model = "Keins"
        llm.run_page_fusion.return_value = ""
        orch = PipelineOrchestrator(config=config, llm_client=llm, organize_enabled=False)

        fused = orch._stage_fusion(
            image_paths=[Path("page1.png")],
            ocr_texts={1: "ocr text"},
            vision_markdowns={1: "vision text"},
            page_markdowns={1: "docling text"},
            glm_texts={1: "glm text"},
            total_pages=1,
        )

        assert fused == {1: "vision text"}


def test_process_file_uses_document_exporter_returned_paths_for_gdrive():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        config = AppConfig(root)
        config.original_dir.mkdir(parents=True, exist_ok=True)
        config.work_dir.mkdir(parents=True, exist_ok=True)
        config.final_dir.mkdir(parents=True, exist_ok=True)
        exported_pdf = config.final_dir / "real_exported.pdf"
        exported_docx = config.final_dir / "begleitdateien" / "real_exported.docx"
        exported_json = config.final_dir / "begleitdateien" / "real_exported_quality_report.json"
        exported_docx.parent.mkdir(parents=True, exist_ok=True)
        exported_pdf.write_text("pdf", encoding="utf-8")
        exported_docx.write_text("docx", encoding="utf-8")
        exported_json.write_text("json", encoding="utf-8")

        llm = MagicMock()
        llm.vision_model = "Keins"
        llm.glm_ocr_model = "Keins"
        llm.fusion_model = "Keins"
        llm.analysis_model = "Keins"

        orch = PipelineOrchestrator(
            config=config,
            llm_client=llm,
            organize_enabled=False,
            gdrive_enabled=True,
            gdrive_upload_pdf=True,
            gdrive_upload_docx=True,
            gdrive_upload_json=True,
            save_docx_enabled=True,
            save_json_enabled=True,
        )
        orch._stage_prepare = MagicMock(return_value=root / "work.pdf")
        orch._stage_ocrmypdf = MagicMock(return_value=(root / "ocr.pdf", "ocr text"))
        orch._stage_docling = MagicMock(return_value=("docling text", {}))
        orch._stage_extract_pages = MagicMock(return_value=([], {}))
        orch._stage_fusion = MagicMock(return_value={1: "fused text"})
        orch._stage_quality = MagicMock(return_value=("fused text", {"warnings": []}))
        # Keep this integration test focused on exported-path propagation.
        # A supported title satisfies the independent metadata-evidence gate.
        orch._stage_analysis = MagicMock(return_value=({"title": "fused text"}, "final_name"))
        orch._stage_export = MagicMock(return_value={
            "pdf": exported_pdf,
            "docx": exported_docx,
            "json": exported_json,
            "txt": None,
        })
        orch._stage_gdrive_upload = MagicMock()

        source = root / "input.pdf"
        source.write_text("input", encoding="utf-8")
        outcome = orch.process_file(source)

        upload_kwargs = orch._stage_gdrive_upload.call_args.kwargs
        assert upload_kwargs["pdf_file"] == exported_pdf
        assert upload_kwargs["docx_file"] == exported_docx
        assert upload_kwargs["json_file"] == exported_json


def test_process_file_does_not_overwrite_existing_original_or_error_files():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        config = AppConfig(root)
        config.ensure_directories()
        (config.original_dir / "input.pdf").write_text("existing original", encoding="utf-8")
        (config.error_dir / "input_001.pdf").write_text("existing error", encoding="utf-8")
        source = root / "input.pdf"
        source.write_text("new input", encoding="utf-8")

        llm = MagicMock()
        llm.vision_model = "Keins"
        llm.glm_ocr_model = "Keins"
        llm.fusion_model = "Keins"
        llm.analysis_model = "Keins"

        orch = PipelineOrchestrator(config=config, llm_client=llm, organize_enabled=False)
        orch._stage_prepare = MagicMock(side_effect=RuntimeError("forced failure"))

        outcome = orch.process_file(source)

        assert (config.original_dir / "input.pdf").read_text(encoding="utf-8") == "existing original"
        assert (config.error_dir / "input_001.pdf").read_text(encoding="utf-8") == "existing error"
        job_evidence = [path for path in config.error_dir.iterdir() if path.is_dir()]
        assert len(job_evidence) == 1
        preserved_inputs = list(job_evidence[0].glob("input_001*.pdf"))
        assert len(preserved_inputs) == 1
        assert preserved_inputs[0].read_text(encoding="utf-8") == "new input"
        assert (job_evidence[0] / "job_manifest.json").is_file()
        assert (job_evidence[0] / "debug_report.json").is_file()
        assert not source.exists()
        assert outcome["status"] == "failed"
        assert "forced failure" in outcome["error"]
