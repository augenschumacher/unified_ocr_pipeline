import base64
import hashlib
from unittest.mock import MagicMock, patch

import pytest

from core.cloud.gdrive_client import GoogleDriveClient
from core.cloud.synology_client import SynologyWebDAVClient


@patch("core.cloud.gdrive_client.MediaFileUpload")
@patch("core.cloud.gdrive_client.GoogleDriveClient._get_service")
@patch("core.cloud.gdrive_client.GoogleDriveClient._resolve_path_to_folder_id")
def test_gdrive_reuses_exact_duplicate_without_any_write(
    mock_resolve, mock_get_service, mock_media, tmp_path
):
    local = tmp_path / "record.pdf"
    local.write_bytes(b"archival record")
    checksum = hashlib.md5(local.read_bytes()).hexdigest()
    mock_resolve.return_value = "folder-id"
    service = MagicMock()
    mock_get_service.return_value = service
    service.files().list().execute.return_value = {
        "files": [
            {
                "id": "same-id",
                "name": local.name,
                "md5Checksum": checksum,
                "size": str(local.stat().st_size),
                "mimeType": "application/pdf",
            }
        ]
    }

    result = GoogleDriveClient().upload_file_with_audit(
        "token.json", str(local), "Archiv/2026"
    )

    assert result["action"] == "duplicate"
    assert result["drive_file_id"] == "same-id"
    assert result["content_md5"] == checksum
    service.files().create.assert_not_called()
    service.files().update.assert_not_called()
    mock_media.assert_not_called()


@patch("core.cloud.gdrive_client.MediaFileUpload")
@patch("core.cloud.gdrive_client.GoogleDriveClient._get_service")
@patch("core.cloud.gdrive_client.GoogleDriveClient._resolve_path_to_folder_id")
def test_gdrive_different_content_gets_first_free_conflict_name(
    mock_resolve, mock_get_service, mock_media, tmp_path
):
    local = tmp_path / "record.pdf"
    local.write_bytes(b"new content")
    mock_resolve.return_value = "folder-id"
    service = MagicMock()
    mock_get_service.return_value = service
    service.files().list().execute.side_effect = [
        {"files": [{"id": "original-id", "md5Checksum": "0" * 32}]},
        {"files": [{"id": "conflict-1-id", "md5Checksum": "1" * 32}]},
        {"files": []},
        {"files": [{"id": "new-id", "name": "record_conflict_002.pdf"}]},
    ]
    service.files().create().execute.return_value = {"id": "new-id"}
    def created_metadata():
        body = service.files().create.call_args.kwargs["body"]
        return {
            "id": "new-id",
            "name": body["name"],
            "parents": ["folder-id"],
            "md5Checksum": hashlib.md5(local.read_bytes()).hexdigest(),
            "size": str(local.stat().st_size),
            "mimeType": "application/pdf",
            "trashed": False,
            "appProperties": body["appProperties"],
        }
    service.files().get.return_value.execute.side_effect = created_metadata

    result = GoogleDriveClient().upload_file_with_audit(
        "token.json", str(local), "Archiv/2026"
    )

    assert result["action"] == "created_conflict"
    assert result["remote_filename"] == "record_conflict_002.pdf"
    assert result["conflict_with_ids"] == ["original-id"]
    assert service.files().create.call_args.kwargs["body"]["name"] == result["remote_filename"]
    service.files().update.assert_not_called()


class _Response:
    def __init__(self, status_code, *, text="", headers=None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}


class _WebDAVSession:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return next(self.responses)


def _synology(session):
    return SynologyWebDAVClient(
        base_url="https://nas.local:5006",
        username="user",
        password="secret",
        session=session,
    )


def test_synology_unknown_existing_content_is_not_overwritten(tmp_path):
    local = tmp_path / "record.pdf"
    local.write_bytes(b"new content")
    session = _WebDAVSession(
        [
            _Response(207),  # original name exists, but exposes no trustworthy digest
            _Response(404),  # first conflict name is free
            _Response(201),  # conditional create succeeds
        ]
    )

    result = _synology(session).upload_file(local, "")

    assert result["action"] == "uploaded_conflict"
    assert result["remote_filename"] == "record_conflict_001.pdf"
    put_calls = [call for call in session.calls if call[0] == "PUT"]
    assert len(put_calls) == 1
    assert put_calls[0][1].endswith("/record_conflict_001.pdf")
    assert put_calls[0][2]["headers"]["If-None-Match"] == "*"


def test_synology_reuses_exact_digest_duplicate_without_put(tmp_path):
    local = tmp_path / "record.pdf"
    local.write_bytes(b"same content")
    content_md5 = base64.b64encode(hashlib.md5(local.read_bytes()).digest()).decode("ascii")
    session = _WebDAVSession([_Response(207, headers={"Content-MD5": content_md5})])

    result = _synology(session).upload_file(local, "")

    assert result["action"] == "duplicate"
    assert result["remote_filename"] == local.name
    assert not any(call[0] == "PUT" for call in session.calls)


def test_synology_race_advances_to_conflict_name(tmp_path):
    local = tmp_path / "record.pdf"
    local.write_bytes(b"content")
    session = _WebDAVSession(
        [
            _Response(404),  # original looked free
            _Response(412),  # another writer claimed it before our conditional PUT
            _Response(404),  # conflict name is free
            _Response(201),
        ]
    )

    result = _synology(session).upload_file(local, "")

    assert result["action"] == "uploaded_conflict"
    assert result["remote_filename"] == "record_conflict_001.pdf"
    assert len([call for call in session.calls if call[0] == "PUT"]) == 2


def test_synology_blocks_ambiguous_put_result_instead_of_claiming_update(tmp_path):
    local = tmp_path / "record.pdf"
    local.write_bytes(b"content")
    session = _WebDAVSession([_Response(404), _Response(204)])

    with pytest.raises(RuntimeError, match="stillen Ueberschreibens"):
        _synology(session).upload_file(local, "")
