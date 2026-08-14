from pathlib import Path
from unittest.mock import MagicMock, patch

from core.cloud.classifier import classify_document
from core.cloud.gdrive_client import GoogleDriveClient
from core.cloud.organizer import DocumentOrganizer
from core.config import AppConfig
from core.pipeline import PipelineOrchestrator
from core.settings import SettingsManager


def test_document_organizer_never_silently_overwrites_existing_file(tmp_path):
    final_dir = tmp_path / "final"
    target_dir = final_dir / "Fabio" / "Auto"
    target_dir.mkdir(parents=True)
    final_dir.mkdir(exist_ok=True)

    incoming = final_dir / "2026-06-20_Service_Rechnung.pdf"
    existing = target_dir / incoming.name
    incoming.write_text("new document", encoding="utf-8")
    existing.write_text("existing document", encoding="utf-8")

    organizer = DocumentOrganizer(final_dir)
    moved = organizer.organize("2026-06-20_Service_Rechnung", "Fabio/Auto")

    assert existing.read_text(encoding="utf-8") == "existing document"
    assert len(moved) == 1
    assert moved[0].name.startswith("2026-06-20_Service_Rechnung_conflict_")
    assert moved[0].read_text(encoding="utf-8") == "new document"
    assert any(entry["action"] == "name_conflict" for entry in organizer.last_audit)


def test_document_organizer_rejects_path_traversal_targets(tmp_path):
    final_dir = tmp_path / "final"
    final_dir.mkdir()
    incoming = final_dir / "doc.pdf"
    incoming.write_text("pdf", encoding="utf-8")

    organizer = DocumentOrganizer(final_dir)
    moved = organizer.organize("doc", "../outside")

    assert moved == [final_dir / "Sonstiges" / "doc.pdf"]
    assert moved[0].exists()
    assert not (tmp_path / "outside" / "doc.pdf").exists()
    assert any(entry["action"] == "target_path_sanitized" for entry in organizer.last_audit)


def test_gui_review_completion_uses_configured_remote_sync(tmp_path):
    from app import App

    class _Var:
        def __init__(self, value):
            self.value = value

        def get(self):
            return self.value

    class _Runner:
        def __init__(self):
            self.calls = []
            self.events = []
            self.gdrive_enabled = True
            self.synology_enabled = False
            self._last_google_drive_summary = None
            self._last_synology_summary = None

        def report_workflow_status(self, step, state, message, details=None):
            self.events.append((step, state, message, details))

        def _stage_gdrive_upload(self, pdf, docx, quality, target, is_docx_input=False):
            self.calls.append((pdf, docx, quality, target, is_docx_input))
            self._last_google_drive_summary = {
                "state": "success",
                "message": "Google Drive bestätigt.",
                "details": {"confirmed_count": 1, "expected_count": 1},
            }
            return [{"provider": "google_drive", "action": "created"}]

        def _stage_synology_upload(self, *_args, **_kwargs):
            raise AssertionError("Synology ist im Test deaktiviert")

    app = object.__new__(App)
    app.gdrive_enabled_var = _Var(True)
    app.synology_enabled_var = _Var(False)
    runner = _Runner()
    app._build_pipeline_orchestrator = lambda _config: runner
    pdf = tmp_path / "reviewed.pdf"
    txt = tmp_path / "reviewed.txt"
    heartbeat_calls = []
    pdf.write_bytes(b"pdf")
    txt.write_text("Text", encoding="utf-8")

    audit = app._sync_review_artifacts(
        AppConfig(str(tmp_path / "archive")),
        {
            "target_path": "Fabio/Finanzen",
            "artifacts": {"pdf": str(pdf), "txt": str(txt)},
            "heartbeat": lambda: heartbeat_calls.append("beat"),
        },
        {"payload": {"is_docx": False}},
    )

    assert audit == [{"provider": "google_drive", "action": "created"}]
    assert runner.calls == [(pdf, None, None, "Fabio/Finanzen", False)]
    assert heartbeat_calls == ["beat", "beat"]


def test_enabled_review_sync_without_confirmation_stays_recoverable(tmp_path):
    from app import App

    class _Runner:
        gdrive_enabled = True
        synology_enabled = False
        _last_google_drive_summary = None
        _last_synology_summary = None

        def __init__(self):
            self.events = []

        def report_workflow_status(self, step, state, message, details=None):
            self.events.append((step, state, message, details))

        def _stage_gdrive_upload(self, *_args, **_kwargs):
            self._last_google_drive_summary = {
                "state": "skipped",
                "message": "Keine Upload-Datei ausgewählt.",
                "details": {"confirmed_count": 0, "expected_count": 0},
            }
            return []

    app = object.__new__(App)
    runner = _Runner()
    pdf = tmp_path / "reviewed.pdf"
    pdf.write_bytes(b"pdf")

    audit = app._sync_review_artifacts(
        AppConfig(str(tmp_path / "archive")),
        {
            "target_path": "Fabio/Finanzen",
            "artifacts": {"pdf": str(pdf)},
        },
        {"job_id": "job-review", "payload": {"is_docx": False}},
        runner=runner,
    )

    assert audit[-1]["action"] == "failed"
    assert "google_drive" in audit[-1]["error"]
    assert runner.events[-1][0:2] == ("complete", "error")


@patch("core.cloud.gdrive_client.MediaFileUpload")
@patch("core.cloud.gdrive_client.GoogleDriveClient._get_service")
@patch("core.cloud.gdrive_client.GoogleDriveClient._resolve_path_to_folder_id")
def test_gdrive_upload_audit_distinguishes_created_and_safe_conflict(mock_resolve, mock_get_service, mock_media, tmp_path):
    local_file = tmp_path / "bericht.pdf"
    local_file.write_text("pdf", encoding="utf-8")
    mock_resolve.return_value = "folder_id"

    service = MagicMock()
    mock_get_service.return_value = service
    service.files().list().execute.side_effect = [
        {"files": []},
        {"files": [{"id": "new-id", "name": "bericht.pdf"}]},
    ]
    service.files().create().execute.return_value = {"id": "new-id"}
    def created_metadata():
        body = service.files().create.call_args.kwargs["body"]
        return {
            "id": service.files().create().execute.return_value["id"],
            "name": body["name"],
            "parents": ["folder_id"],
            "md5Checksum": GoogleDriveClient._md5_file(local_file),
            "size": str(local_file.stat().st_size),
            "mimeType": "application/pdf",
            "trashed": False,
            "appProperties": body["appProperties"],
        }
    service.files().get.return_value.execute.side_effect = created_metadata

    client = GoogleDriveClient()
    created = client.upload_file_with_audit("token.json", str(local_file), "Fabio/Gesundheit")

    assert created["action"] == "created"
    assert created["drive_file_id"] == "new-id"
    assert created["provider"] == "google_drive"

    service.files().list().execute.side_effect = [
        {"files": [{"id": "existing-id", "name": local_file.name}]},
        {"files": []},
        {"files": [{"id": "conflict-id", "name": "bericht_conflict_001.pdf"}]},
    ]
    service.files().create().execute.return_value = {"id": "conflict-id"}

    conflict = client.upload_file_with_audit("token.json", str(local_file), "Fabio/Gesundheit")

    assert conflict["action"] == "created_conflict"
    assert conflict["drive_file_id"] == "conflict-id"
    assert conflict["remote_filename"] == "bericht_conflict_001.pdf"
    service.files().update.assert_not_called()


def test_pipeline_gdrive_stage_preserves_drive_audit_action(tmp_path):
    pdf = tmp_path / "out.pdf"
    pdf.write_text("pdf", encoding="utf-8")

    orch = PipelineOrchestrator(
        config=AppConfig(tmp_path),
        llm_client=MagicMock(),
        gdrive_enabled=True,
        gdrive_token_path=str(tmp_path / "token.json"),
        gdrive_upload_pdf=True,
    )

    class FakeDriveClient:
        def is_authenticated(self, token_path):
            return True

        def upload_file_with_audit(self, token_path, local_path, relative_dest_path):
            return {
                "provider": "google_drive",
                "local_path": local_path,
                "filename": Path(local_path).name,
                "drive_file_id": "drive-id",
                "folder_path": relative_dest_path,
                "action": "created",
            }

    with patch("core.cloud.gdrive_client.GoogleDriveClient", return_value=FakeDriveClient()):
        uploads = orch._stage_gdrive_upload(
            pdf_file=pdf,
            docx_file=tmp_path / "missing.docx",
            json_file=tmp_path / "missing.json",
            target_path="Fabio/Gesundheit",
        )

    assert uploads == [{
        "provider": "google_drive",
        "local_path": str(pdf),
        "filename": "out.pdf",
        "drive_file_id": "drive-id",
        "folder_path": "Fabio/Gesundheit",
        "action": "created",
    }]


def test_classifier_returns_reader_facing_explanation():
    class MockLLM:
        analysis_model = "mock-model"
        fusion_model = None

        def query(self, model, system_prompt, user_prompt, think=False, **kwargs):
            return '{"recommended_path": "Fabio/Auto/Golf", "confidence": 82, "reason": "Kennzeichen und Service erkannt"}'

    result = classify_document(
        "Inspektion fuer Golf AB CD 123",
        {},
        ["Fabio/Auto/Golf", "Sonstiges"],
        MockLLM(),
        ["Fabio", "Sonstiges"],
    )

    assert result["recommended_path"] == "Fabio/Auto/Golf"
    assert result["evidence"] == ["Kennzeichen und Service erkannt"]
    assert "LLM-Klassifikation" in result["explanation"]


def test_large_pdf_page_limit_is_configurable_and_bounded(tmp_path):
    config = AppConfig(tmp_path, large_pdf_page_limit=7)
    assert config.large_pdf_page_limit == 7
    assert AppConfig(tmp_path, large_pdf_page_limit=0).large_pdf_page_limit == 1
    assert AppConfig(tmp_path, large_pdf_page_limit=5000).large_pdf_page_limit == 1000

    settings_path = tmp_path / "settings.json"
    manager = SettingsManager(settings_path)
    data = manager.settings.copy()
    data["large_pdf_page_limit"] = 12
    manager.save(data)

    assert SettingsManager(settings_path).settings["large_pdf_page_limit"] == 12
