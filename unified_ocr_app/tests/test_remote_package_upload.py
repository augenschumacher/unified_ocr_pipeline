import base64
import hashlib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.cloud.folder_registry import FolderRegistry
from core.cloud.gdrive_client import GoogleDriveClient
from core.cloud.synology_client import SynologyWebDAVClient
from core.config import AppConfig
from core.pipeline import PipelineOrchestrator


def _drive_item(
    file_id: str,
    name: str,
    content: bytes,
    *,
    parent: str = "folder-id",
    app_properties: dict | None = None,
) -> dict:
    item = {
        "id": file_id,
        "name": name,
        "parents": [parent],
        "md5Checksum": hashlib.md5(content).hexdigest(),
        "size": str(len(content)),
        "mimeType": "application/octet-stream",
        "trashed": False,
    }
    if app_properties is not None:
        item["appProperties"] = app_properties
    return item


def test_gdrive_package_uses_one_conflict_stem_and_resumes_partial_upload(tmp_path):
    pdf = tmp_path / "record.pdf"
    report = tmp_path / "record_quality_report.json"
    pdf.write_bytes(b"new pdf")
    report.write_bytes(b"new report")

    remote = {
        "record.pdf": [_drive_item("old", "record.pdf", b"old pdf")],
        # Simulates a previous partial attempt under the first package-wide
        # conflict stem: the PDF is already there, the report is not.
        "record_conflict_001.pdf": [
            _drive_item("partial", "record_conflict_001.pdf", pdf.read_bytes())
        ],
    }
    created = []
    known_seen = {}
    client = GoogleDriveClient()
    client._get_service = MagicMock(return_value=object())

    def resolve(_service, path, known_ids=None):
        known_seen.update(known_ids or {})
        assert path == "Archiv/2026"
        return "folder-id"

    client._resolve_path_to_folder_id = resolve
    client._find_files = lambda _service, name, _parent: list(remote.get(name, []))

    def create(_service, local, name, _parent, *, app_properties=None):
        created.append(name)
        remote[name] = [
            _drive_item(
                f"new-{len(created)}",
                name,
                local.read_bytes(),
                app_properties=app_properties,
            )
        ]
        return remote[name][0]["id"], "application/octet-stream"

    client._create_remote_file = create
    client._get_file_metadata = lambda _service, file_id: next(
        item
        for entries in remote.values()
        for item in entries
        if item["id"] == file_id
    )
    audit = client.upload_package_with_audit(
        "token.json",
        {"pdf": pdf, "quality_report": report},
        "Archiv/2026",
        known_ids={"Archiv": "known-root"},
    )

    assert known_seen == {"Archiv": "known-root"}
    assert [entry["remote_filename"] for entry in audit] == [
        "record_conflict_001.pdf",
        "record_conflict_001_quality_report.json",
    ]
    assert [entry["action"] for entry in audit] == ["duplicate", "created_conflict"]
    assert created == ["record_conflict_001_quality_report.json"]

    # The next retry is a pure idempotent reuse and performs no write.
    retry = client.upload_package_with_audit(
        "token.json",
        {"pdf": pdf, "quality_report": report},
        "Archiv/2026",
        known_ids={"Archiv": "known-root"},
    )
    assert [entry["action"] for entry in retry] == ["duplicate", "duplicate"]
    assert created == ["record_conflict_001_quality_report.json"]


def test_gdrive_resolver_delegates_to_validated_folder_resolution():
    client = GoogleDriveClient()
    service = object()
    client.ensure_folder_path = MagicMock(
        return_value={"folder_id": "validated-id", "path_ids": {}}
    )

    result = client._resolve_path_to_folder_id(
        service,
        "Archiv/2026",
        known_ids={"Archiv": "persisted-id"},
    )

    assert result == "validated-id"
    client.ensure_folder_path.assert_called_once_with(
        service,
        "Archiv/2026",
        known_ids={"Archiv": "persisted-id"},
    )


def test_gdrive_post_create_name_race_rolls_back_own_tagged_file(tmp_path):
    pdf = tmp_path / "record.pdf"
    pdf.write_bytes(b"archival pdf")
    service = MagicMock()
    client = GoogleDriveClient()
    client._get_service = MagicMock(return_value=service)
    client._resolve_path_to_folder_id = MagicMock(return_value="folder-id")
    remote: dict[str, list[dict]] = {}
    by_id: dict[str, dict] = {}
    created_properties: list[dict] = []

    client._find_files = lambda _service, name, _parent: list(remote.get(name, []))

    def create(_service, local, name, parent, *, app_properties=None):
        file_id = f"ours-{len(created_properties) + 1}"
        created_properties.append(dict(app_properties or {}))
        ours = _drive_item(
            file_id,
            name,
            local.read_bytes(),
            parent=parent,
            app_properties=app_properties,
        )
        by_id[file_id] = ours
        remote[name] = [ours]
        if name == "record.pdf":
            # Another writer wins the same visible name between our preflight
            # and the post-create uniqueness check.
            remote[name].append(_drive_item("racer", name, b"other", parent=parent))
        return file_id, "application/pdf"

    client._create_remote_file = create
    client._get_file_metadata = lambda _service, file_id: by_id[file_id]

    audit = client.upload_package_with_audit("token.json", {"pdf": pdf}, "Archiv")

    assert audit[0]["remote_filename"] == "record_conflict_001.pdf"
    assert audit[0]["post_create_verified"] is True
    assert audit[0]["rollback_audit"] == [
        {
            "drive_file_id": "ours-1",
            "remote_filename": "record.pdf",
            "action": "rolled_back",
        }
    ]
    service.files().delete.assert_called_once_with(fileId="ours-1")
    for properties in created_properties:
        assert properties["unifiedOcrPackage"]
        assert properties["unifiedOcrRole"] == "pdf"
        assert properties["unifiedOcrMd5"] == hashlib.md5(pdf.read_bytes()).hexdigest()
        assert properties["unifiedOcrAttempt"]


class _Response:
    def __init__(self, status_code, *, text="", headers=None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}


class _MemorySynology(SynologyWebDAVClient):
    def __init__(self, remote):
        super().__init__(
            base_url="https://nas.local:5006",
            username="user",
            password="secret",
        )
        self.remote = dict(remote)
        self.puts = []

    def ensure_folder(self, _relative_path):
        return []

    def _request(self, method, url, **kwargs):
        name = url.rsplit("/", 1)[-1]
        if method == "PROPFIND":
            if name not in self.remote:
                return _Response(404)
            digest = base64.b64encode(hashlib.md5(self.remote[name]).digest()).decode("ascii")
            return _Response(207, headers={"Content-MD5": digest})
        if method == "PUT":
            if name in self.remote:
                return _Response(412)
            self.remote[name] = kwargs["data"].read()
            self.puts.append(name)
            return _Response(201)
        raise AssertionError(method)


class _RollbackRaceSynology(SynologyWebDAVClient):
    def __init__(self, *, failure_status: int):
        super().__init__(
            base_url="https://nas.local:5006",
            username="user",
            password="secret",
        )
        self.failure_status = failure_status
        self.remote: dict[str, bytes] = {}
        self.etags: dict[str, str] = {}
        self.deleted: list[tuple[str, str]] = []
        self.failed_once = False

    def ensure_folder(self, _relative_path):
        return []

    def _request(self, method, url, **kwargs):
        name = url.rsplit("/", 1)[-1]
        if method == "PROPFIND":
            if name not in self.remote:
                return _Response(404)
            digest = base64.b64encode(hashlib.md5(self.remote[name]).digest()).decode("ascii")
            return _Response(
                207,
                headers={"Content-MD5": digest, "ETag": self.etags[name]},
            )
        if method == "PUT":
            if name == "record_job_manifest.json" and not self.failed_once:
                self.failed_once = True
                return _Response(self.failure_status, text="injected race/failure")
            if name in self.remote:
                return _Response(412)
            self.remote[name] = kwargs["data"].read()
            self.etags[name] = f'"etag-{name}"'
            return _Response(201, headers={"ETag": self.etags[name]})
        if method == "DELETE":
            if_match = kwargs.get("headers", {}).get("If-Match")
            if name not in self.remote:
                return _Response(404)
            if if_match != self.etags[name]:
                return _Response(412)
            self.deleted.append((name, if_match))
            del self.remote[name]
            del self.etags[name]
            return _Response(204)
        raise AssertionError(method)


def test_synology_package_uses_one_conflict_stem_and_resumes_partial_upload(tmp_path):
    pdf = tmp_path / "record.pdf"
    manifest = tmp_path / "record_job_manifest.json"
    pdf.write_bytes(b"new pdf")
    manifest.write_bytes(b"new manifest")
    client = _MemorySynology({
        "record.pdf": b"old pdf",
        "record_conflict_001.pdf": pdf.read_bytes(),
    })

    audit = client.upload_package_with_audit(
        {"pdf": pdf, "job_manifest": manifest},
        "Archiv/2026",
    )

    assert [entry["remote_filename"] for entry in audit] == [
        "record_conflict_001.pdf",
        "record_conflict_001_job_manifest.json",
    ]
    assert [entry["action"] for entry in audit] == ["duplicate", "uploaded_conflict"]
    assert client.puts == ["record_conflict_001_job_manifest.json"]

    retry = client.upload_package_with_audit(
        {"pdf": pdf, "job_manifest": manifest},
        "Archiv/2026",
    )
    assert [entry["action"] for entry in retry] == ["duplicate", "duplicate"]
    assert client.puts == ["record_conflict_001_job_manifest.json"]


def test_synology_late_412_rolls_back_attempt_then_uses_one_conflict_stem(tmp_path):
    pdf = tmp_path / "record.pdf"
    manifest = tmp_path / "record_job_manifest.json"
    pdf.write_bytes(b"pdf")
    manifest.write_bytes(b"manifest")
    client = _RollbackRaceSynology(failure_status=412)

    audit = client.upload_package_with_audit(
        {"pdf": pdf, "job_manifest": manifest}, "Archiv"
    )

    assert [entry["remote_filename"] for entry in audit] == [
        "record_conflict_001.pdf",
        "record_conflict_001_job_manifest.json",
    ]
    assert "record.pdf" not in client.remote
    assert client.deleted == [("record.pdf", '"etag-record.pdf"')]
    assert audit[0]["rollback_audit"] == [
        {
            "remote_path": "Archiv/record.pdf",
            "action": "rolled_back",
            "if_match": '"etag-record.pdf"',
        }
    ]


def test_synology_late_http_error_rolls_back_and_exposes_audit(tmp_path):
    pdf = tmp_path / "record.pdf"
    manifest = tmp_path / "record_job_manifest.json"
    pdf.write_bytes(b"pdf")
    manifest.write_bytes(b"manifest")
    client = _RollbackRaceSynology(failure_status=500)

    with pytest.raises(RuntimeError, match="Rollback vollstaendig") as caught:
        client.upload_package_with_audit(
            {"pdf": pdf, "job_manifest": manifest}, "Archiv"
        )

    assert "record.pdf" not in client.remote
    assert caught.value.rollback_audit == [
        {
            "remote_path": "Archiv/record.pdf",
            "action": "rolled_back",
            "if_match": '"etag-record.pdf"',
        }
    ]

def test_pipeline_drive_batch_receives_registry_ids(tmp_path):
    pdf = tmp_path / "record.pdf"
    report = tmp_path / "record_quality_report.json"
    pdf.write_bytes(b"pdf")
    report.write_bytes(b"report")
    registry = FolderRegistry(tmp_path)
    registry.add_path("Archiv/2026")
    registry.set_drive_folder_id("Archiv", "root-id")
    registry.set_drive_folder_id("Archiv/2026", "target-id")
    registry.save()

    runner = PipelineOrchestrator(
        config=AppConfig(tmp_path),
        llm_client=MagicMock(),
        gdrive_enabled=True,
        gdrive_token_path=str(tmp_path / "token.json"),
        gdrive_upload_pdf=True,
        gdrive_upload_json=True,
    )

    class FakeClient:
        def __init__(self):
            self.call = None

        def is_authenticated(self, _token):
            return True

        def upload_package_with_audit(self, token, paths, target, *, known_ids=None):
            self.call = (token, paths, target, known_ids)
            return [{"provider": "google_drive", "action": "created"}]

    client = FakeClient()
    with patch("core.cloud.gdrive_client.GoogleDriveClient", return_value=client):
        audit = runner._stage_gdrive_upload(
            pdf,
            tmp_path / "missing.docx",
            report,
            "Archiv/2026",
        )

    assert audit == [{"provider": "google_drive", "action": "created"}]
    assert client.call[1] == {"pdf": pdf, "quality_report": report}
    assert client.call[2] == "Archiv/2026"
    assert client.call[3]["Archiv/2026"] == "target-id"
