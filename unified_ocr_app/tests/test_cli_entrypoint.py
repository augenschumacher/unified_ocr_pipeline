from pathlib import Path

import pytest

import main as cli_main


def _settings(tmp_path: Path, extra_input: Path) -> dict:
    return {
        "base_dir": str(tmp_path / "base"),
        "additional_consume_dirs": [str(extra_input)],
        "large_pdf_page_limit": 7,
        "models": {
            "vision": "gemini/gemini-2.5-flash",
            "fusion": "ollama/gemma4:e4b-it-qat",
            "analysis": "ollama/gemma4:e4b-it-qat",
            "glm_ocr": "ollama/glm-ocr:bf16",
        },
        "prompts": {"fusion": "prompt"},
        "unload_models_enabled": False,
        "think_fusion": True,
        "think_analysis": False,
        "redact_cloud_inputs": True,
        "output_format": "PDF und DOCX",
        "docx_mode": "Lesbare DOCX",
        "organize_enabled": False,
        "gdrive_enabled": True,
        "gdrive_token_path": "token.json",
        "save_docx_enabled": False,
        "save_json_enabled": False,
        "gdrive_upload_pdf": True,
        "gdrive_upload_docx": True,
        "gdrive_upload_json": False,
        "synology_enabled": True,
        "synology_base_url": "https://nas.local:5006",
        "synology_username": "user",
        "synology_password": "secret",
        "synology_root_path": "/OCR",
        "synology_upload_pdf": True,
        "synology_upload_docx": False,
        "synology_upload_json": True,
        "large_pdf_reduced": False,
        "ocr_languages": "deu+fra",
        "ocr_mode": "redo",
        "privacy_mode": "local_only",
        "debug_artifacts_enabled": False,
        "prompt_version": 4,
    }


def test_cli_passes_gui_safety_and_sync_settings_to_pipeline(tmp_path, monkeypatch):
    source = tmp_path / "scan.pdf"
    source.write_bytes(b"%PDF-1.4")
    extra_input = tmp_path / "scanner"
    settings = _settings(tmp_path, extra_input)
    captured_llm = {}
    captured_pipeline = {}
    processed = []

    class FakeSettingsManager:
        def load(self):
            return settings

    class FakeLLMClient:
        def __init__(self, **kwargs):
            captured_llm.update(kwargs)

    class FakePipelineOrchestrator:
        def __init__(self, **kwargs):
            captured_pipeline.update(kwargs)

        def process_file(self, path):
            processed.append(path)

    monkeypatch.setattr(cli_main, "SettingsManager", lambda: FakeSettingsManager())
    monkeypatch.setattr(cli_main, "LLMClient", FakeLLMClient)
    monkeypatch.setattr(cli_main, "PipelineOrchestrator", FakePipelineOrchestrator)

    cli_main.run_cli(file_path=str(source), force=True)

    assert len(processed) == 1
    assert processed[0].parent == Path(settings["base_dir"]) / "consume"
    assert source.exists()
    assert captured_llm["force_pipeline"] is True
    assert captured_llm["redact_cloud_inputs"] is True
    assert captured_llm["keep_alive"] == "15m"
    assert captured_llm["prompt_version"] == 4

    config = captured_pipeline["config"]
    assert config.large_pdf_page_limit == 7
    assert extra_input in config.consume_dirs
    assert captured_pipeline["privacy_mode"] == "local_only"
    assert captured_pipeline["debug_artifacts_enabled"] is False
    assert captured_pipeline["synology_enabled"] is True
    assert captured_pipeline["synology_root_path"] == "/OCR"
    assert captured_pipeline["gdrive_upload_docx"] is True
    assert captured_pipeline["large_pdf_reduced"] is False
    assert captured_pipeline["ocr_languages"] == "deu+fra"
    assert captured_pipeline["ocr_mode"] == "redo"


def test_cli_rejects_unsupported_single_file_before_pipeline_setup(tmp_path, monkeypatch):
    source = tmp_path / "notes.txt"
    source.write_text("not a supported OCR input", encoding="utf-8")
    settings = _settings(tmp_path, tmp_path / "scanner")

    class FakeSettingsManager:
        def load(self):
            return settings

    def fail_if_called(*args, **kwargs):
        raise AssertionError("pipeline should not be built for unsupported input")

    monkeypatch.setattr(cli_main, "SettingsManager", lambda: FakeSettingsManager())
    monkeypatch.setattr(cli_main, "LLMClient", fail_if_called)
    monkeypatch.setattr(cli_main, "PipelineOrchestrator", fail_if_called)

    with pytest.raises(SystemExit) as exc_info:
        cli_main.run_cli(file_path=str(source))

    assert exc_info.value.code == 1
