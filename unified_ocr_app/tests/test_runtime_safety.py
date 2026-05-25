import json
from pathlib import Path

from core.audit import build_runtime_audit
from core.job_history import JobHistory
from core.runtime_paths import (
    default_token_path,
    normalize_token_path,
    default_credentials_path,
    normalize_credentials_path,
)


class DummyLLM:
    vision_model = "vision-model"
    fusion_model = "fusion-model"
    analysis_model = "analysis-model"
    glm_ocr_model = "glm-model"
    think_fusion = False
    think_analysis = True
    keep_alive = "0"
    prompt_version = 7
    prompts = {
        "fusion": "SECRET PROMPT TEXT",
    }


def test_runtime_audit_uses_prompt_fingerprints_not_prompt_content():
    audit = build_runtime_audit(
        DummyLLM(),
        output_format="PDF und DOCX",
        docx_mode="Lesbare DOCX",
        large_pdf_reduced=True,
    )

    payload = json.dumps(audit, ensure_ascii=False)
    assert "SECRET PROMPT TEXT" not in payload
    assert audit["prompts"]["version"] == 7
    assert audit["prompts"]["fingerprints"]["fusion"]["length"] == len("SECRET PROMPT TEXT")
    assert len(audit["prompts"]["fingerprints"]["fusion"]["sha256"]) == 64


def test_job_history_writes_jsonl_records(tmp_path):
    class Config:
        log_dir = tmp_path / "logs"

    history = JobHistory(Config())
    job_id = history.start(Path("consume") / "doc.pdf")
    history.finish(job_id, "completed", source_name="doc.pdf", final_name="final_doc")

    lines = (tmp_path / "logs" / "job_history.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["event"] == "started"
    assert json.loads(lines[1])["status"] == "completed"


def test_legacy_token_name_maps_to_user_data_dir():
    assert normalize_token_path("token.json") == str(default_token_path())


def test_legacy_credentials_name_maps_to_user_data_dir():
    assert normalize_credentials_path("credentials.json") == str(default_credentials_path())

