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
from app import parse_drop_paths, split_drop_list, supported_input_suffixes_text


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


def test_parse_drop_paths_handles_braced_paths_with_spaces():
    paths = parse_drop_paths("{C:/Users/Fabio/Scan Eingang/rechnung.pdf} C:/Temp/foto.png")

    normalized = [str(path).replace("\\", "/") for path in paths]
    assert normalized == [
        "C:/Users/Fabio/Scan Eingang/rechnung.pdf",
        "C:/Temp/foto.png",
    ]


def test_split_drop_list_keeps_braced_paths_together():
    assert split_drop_list("{C:/a b/1.pdf} {C:/c d/2.pdf}") == [
        "C:/a b/1.pdf",
        "C:/c d/2.pdf",
    ]


def test_split_drop_list_handles_unbraced_and_mixed_entries():
    assert split_drop_list("C:/a.pdf C:/b.pdf") == ["C:/a.pdf", "C:/b.pdf"]
    assert split_drop_list("{C:/ein ordner/x.pdf} C:/y.pdf") == [
        "C:/ein ordner/x.pdf",
        "C:/y.pdf",
    ]


def test_split_drop_list_ignores_empty_input():
    assert split_drop_list("") == []
    assert split_drop_list("   ") == []


def test_parse_drop_paths_works_without_a_tcl_interpreter():
    # Frueher wurde hierfuer ein Wegwerf-tk.Tcl() erzeugt, das sporadisch mit
    # TclError fehlschlug und Pfade mit Leerzeichen zerriss.
    paths = parse_drop_paths("{C:/Scan Eingang/rechnung.pdf} C:/Temp/foto.png")

    assert [str(path).replace("\\", "/") for path in paths] == [
        "C:/Scan Eingang/rechnung.pdf",
        "C:/Temp/foto.png",
    ]


def test_parse_drop_paths_falls_back_when_tk_root_splitlist_fails():
    class BrokenTk:
        class tk:
            @staticmethod
            def splitlist(_raw):
                raise RuntimeError("Tcl nicht verfuegbar")

    paths = parse_drop_paths("{C:/a b/1.pdf}", tk_root=BrokenTk())

    assert [str(path).replace("\\", "/") for path in paths] == ["C:/a b/1.pdf"]


def test_supported_input_suffixes_text_includes_drag_drop_formats():
    text = supported_input_suffixes_text()

    assert ".pdf" in text
    assert ".docx" in text
    assert ".heic" in text
