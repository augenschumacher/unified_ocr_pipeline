import json
from pathlib import Path
from unittest.mock import patch

from core.job_history import JobHistory
from core.local_store import LocalStore
from core.privacy import is_external_model, redact_sensitive_text
from core.system_check import _command_or_module_status, format_system_check, run_system_check


class Config:
    def __init__(self, base_dir: Path):
        self.base_dir = base_dir
        self.log_dir = base_dir / "logs"


def test_local_store_jobs_documents_and_review_queue(tmp_path):
    store = LocalStore(tmp_path)
    source = tmp_path / "doc.pdf"
    source.write_text("pdf", encoding="utf-8")

    store.start_job("job-1", source, "abc")
    store.update_job("job-1", "completed", final_name="final_doc", target_path="Jan/Auto", metadata={"document_type": "Rechnung"})
    store.index_document(
        source_sha256="abc",
        source_name="doc.pdf",
        final_name="final_doc",
        target_path="Jan/Auto",
        outputs={"pdf": "final_doc.pdf"},
        metadata={"document_type": "Rechnung"},
    )

    duplicates = store.find_duplicates("abc")
    assert duplicates[0]["final_name"] == "final_doc"

    item_id = store.add_review_item(
        job_id="job-1",
        kind="sorting_uncertain",
        source_name="doc.pdf",
        proposed_path="Jan/Auto",
        candidates=[{"path": "Jan/Auto", "score": 66}],
        metadata={},
    )
    assert store.list_review_items("pending")[0]["id"] == item_id
    store.resolve_review_item(item_id, "Jan/Auto")
    assert store.list_review_items("pending") == []

    rows = store.search_documents("Auto")
    assert rows[0]["target_path"] == "Jan/Auto"


def test_job_history_mirrors_to_sqlite(tmp_path):
    cfg = Config(tmp_path)
    source = tmp_path / "doc.pdf"
    source.write_text("pdf", encoding="utf-8")

    history = JobHistory(cfg)
    job_id = history.start(source)
    history.finish(job_id, "completed", source_name="doc.pdf", final_name="final_doc", target_path="Jan")

    db_rows = LocalStore(tmp_path).search_documents()
    assert db_rows == []
    lines = (tmp_path / "logs" / "job_history.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[-1])["status"] == "completed"
    assert (tmp_path / "unified_ocr.sqlite3").exists()


def test_redact_sensitive_text_masks_common_identifiers():
    text = "Max, Mustermann IBAN DE02120300000000202051 max@example.de Telefon 030 1234567"
    redacted = redact_sensitive_text(text)

    assert "DE02120300000000202051" not in redacted
    assert "max@example.de" not in redacted
    assert "[IBAN]" in redacted
    assert "[EMAIL]" in redacted
    assert is_external_model("gemini/gemini-2.5-flash")
    assert not is_external_model("qwen3:27b")


def test_system_check_returns_structured_result(tmp_path):
    checks = run_system_check(tmp_path)
    rendered = format_system_check(checks)

    assert "python" in checks
    assert "commands" in checks
    assert "Ordner:" in rendered


def test_system_check_accepts_ocrmypdf_python_module_when_command_missing():
    class Spec:
        origin = "module-origin"

    with patch("core.system_check.shutil.which", return_value=None), \
         patch("core.system_check.importlib.util.find_spec", return_value=Spec()):
        status = _command_or_module_status("ocrmypdf", "ocrmypdf")

    assert status["ok"] is True
    assert status["message"] == "Python-Modul gefunden"
