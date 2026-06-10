import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.config import AppConfig
from core.manifest import JobManifest
from core.pipeline import PipelineOrchestrator


def test_job_manifest_records_stages_outputs_and_drive_uploads():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        source = root / "input.pdf"
        source.write_bytes(b"test input")

        manifest = JobManifest.create(job_id="job-1", source_path=source, manifest_dir=root / "work")
        output = root / "final.pdf"
        output.write_text("pdf", encoding="utf-8")

        manifest.record_stage("export", "ok", artifacts={"pdf": output})
        manifest.record_outputs({"pdf": output})
        manifest.record_drive_uploads(
            enabled=True,
            uploads=[{
                "local_path": str(output),
                "filename": output.name,
                "drive_file_id": "drive-123",
                "folder_path": "Fabio/Gesundheit",
                "action": "uploaded",
            }],
        )
        manifest.record_sync_uploads(
            enabled=True,
            targets={"google_drive": True, "synology_webdav": True},
            uploads=[
                {"provider": "google_drive", "filename": output.name},
                {"provider": "synology_webdav", "filename": output.name},
            ],
        )
        manifest.finalize("completed")

        data = json.loads((root / "work" / "job_manifest.json").read_text(encoding="utf-8"))

        assert data["status"] == "completed"
        assert data["source"]["sha256"]
        assert data["stages"]["export"]["status"] == "ok"
        assert data["outputs"]["pdf"] == str(output)
        assert data["drive"]["uploads"][0]["drive_file_id"] == "drive-123"
        assert data["sync"]["enabled"] is True
        assert data["sync"]["targets"]["synology_webdav"] is True
        assert data["sync"]["uploads"][1]["provider"] == "synology_webdav"


def test_privacy_mode_local_only_disables_external_models_and_gdrive():
    with tempfile.TemporaryDirectory() as tmpdir:
        llm = MagicMock()
        llm.vision_model = "gemini/gemini-2.5-flash"
        llm.fusion_model = "openai/gpt-4o-mini"
        llm.analysis_model = "qwen3.6:27b"
        llm.glm_ocr_model = "mistral/pixtral"

        orch = PipelineOrchestrator(
            config=AppConfig(Path(tmpdir)),
            llm_client=llm,
            gdrive_enabled=True,
            privacy_mode="local_only",
        )
        orch._enforce_privacy_mode()

        assert orch.gdrive_enabled is False
        assert llm.vision_model == "Keins"
        assert llm.fusion_model == "Keins"
        assert llm.analysis_model == "qwen3.6:27b"
        assert llm.glm_ocr_model == "Keins"


def test_privacy_mode_local_only_allows_only_local_synology_targets():
    with tempfile.TemporaryDirectory() as tmpdir:
        local_orch = PipelineOrchestrator(
            config=AppConfig(Path(tmpdir)),
            llm_client=MagicMock(),
            synology_enabled=True,
            synology_base_url="https://nas.local:5006",
            synology_username="user",
            synology_password="secret",
            privacy_mode="local_only",
        )
        public_orch = PipelineOrchestrator(
            config=AppConfig(Path(tmpdir)),
            llm_client=MagicMock(),
            synology_enabled=True,
            synology_base_url="https://example.com/webdav",
            synology_username="user",
            synology_password="secret",
            privacy_mode="local_only",
        )

        assert local_orch.synology_enabled is True
        assert public_orch.synology_enabled is False


def test_gdrive_upload_returns_manifest_ready_audit_entries():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        pdf = root / "out.pdf"
        docx = root / "out.docx"
        report = root / "out.json"
        for path in (pdf, docx, report):
            path.write_text(path.suffix, encoding="utf-8")

        llm = MagicMock()
        orch = PipelineOrchestrator(
            config=AppConfig(root),
            llm_client=llm,
            gdrive_enabled=True,
            gdrive_token_path=str(root / "token.json"),
            gdrive_upload_pdf=True,
            gdrive_upload_docx=True,
            gdrive_upload_json=True,
        )

        client = MagicMock()
        client.is_authenticated.return_value = True
        client.upload_file.side_effect = ["pdf-id", "docx-id", "json-id"]

        with patch("core.cloud.gdrive_client.GoogleDriveClient", return_value=client):
            uploads = orch._stage_gdrive_upload(
                pdf_file=pdf,
                docx_file=docx,
                json_file=report,
                target_path="Fabio/Gesundheit",
            )

        assert [entry["drive_file_id"] for entry in uploads] == ["pdf-id", "docx-id", "json-id"]
        assert all(entry["folder_path"] == "Fabio/Gesundheit" for entry in uploads)
        assert all(entry["action"] == "uploaded" for entry in uploads)
