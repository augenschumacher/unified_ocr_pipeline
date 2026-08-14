import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest
from core.cloud.gdrive_client import GoogleDriveClient

def test_is_authenticated_missing_token():
    client = GoogleDriveClient()
    assert not client.is_authenticated("non_existent_token.json")

@patch("core.cloud.gdrive_client.Credentials")
def test_is_authenticated_valid(mock_creds_class):
    mock_creds = MagicMock()
    mock_creds.valid = True
    mock_creds.expired = False
    mock_creds.refresh_token = None
    mock_creds_class.from_authorized_user_file.return_value = mock_creds
    
    with tempfile.TemporaryDirectory() as tmpdir:
        token_path = Path(tmpdir) / "token.json"
        token_path.write_text("{}", encoding="utf-8")
        
        client = GoogleDriveClient()
        assert client.is_authenticated(str(token_path))

@patch("core.cloud.gdrive_client.build")
@patch("core.cloud.gdrive_client.GoogleDriveClient.get_credentials")
def test_get_authenticated_user_email(mock_get_creds, mock_build):
    mock_get_creds.return_value = MagicMock()
    mock_service = MagicMock()
    mock_build.return_value = mock_service
    mock_service.about().get().execute.return_value = {
        "user": {"emailAddress": "test@gmail.com"}
    }
    
    client = GoogleDriveClient()
    email = client.get_authenticated_user_email("dummy_token.json")
    assert email == "test@gmail.com"

@patch("core.cloud.gdrive_client.build")
def test_resolve_path_to_folder_id_existing(mock_build):
    mock_service = MagicMock()
    mock_build.return_value = mock_service
    
    # Mock folder list queries:
    # 1. Laura exists under root -> ID 'laura_id'
    # 2. Schule exists under 'laura_id' -> ID 'schule_id'
    mock_list = MagicMock()
    mock_service.files().list.return_value = mock_list
    
    mock_list.execute.side_effect = [
        {"files": [{"id": "laura_id", "name": "Laura"}]},
        {"files": [{"id": "schule_id", "name": "Schule"}]}
    ]
    
    client = GoogleDriveClient()
    folder_id = client._resolve_path_to_folder_id(mock_service, "Laura/Schule")
    assert folder_id == "schule_id"
    assert mock_service.files().create.call_count == 0

@patch("core.cloud.gdrive_client.build")
def test_resolve_path_to_folder_id_create(mock_build):
    mock_service = MagicMock()
    mock_build.return_value = mock_service
    
    # Mock list to return empty (folders do not exist yet)
    mock_list = MagicMock()
    mock_service.files().list.return_value = mock_list
    mock_list.execute.return_value = {"files": []}
    
    # Mock create to return new IDs
    mock_create = MagicMock()
    mock_service.files().create.return_value = mock_create
    mock_create.execute.side_effect = [
        {"id": "new_laura_id"},
        {"id": "new_schule_id"}
    ]
    
    client = GoogleDriveClient()
    folder_id = client._resolve_path_to_folder_id(mock_service, "Laura/Schule")
    
    assert folder_id == "new_schule_id"
    assert mock_service.files().create.call_count == 2

@patch("core.cloud.gdrive_client.MediaFileUpload")
@patch("core.cloud.gdrive_client.GoogleDriveClient._get_service")
@patch("core.cloud.gdrive_client.GoogleDriveClient._resolve_path_to_folder_id")
def test_upload_file_new(mock_resolve, mock_get_service, mock_media):
    mock_service = MagicMock()
    mock_get_service.return_value = mock_service
    mock_resolve.return_value = "folder_id"
    
    # Mock list: file doesn't exist
    mock_list = MagicMock()
    mock_service.files().list.return_value = mock_list
    mock_list.execute.side_effect = [
        {"files": []},
        {"files": [{"id": "new_file_id", "name": "rechnung.pdf"}]},
    ]
    
    # Mock create
    mock_create = MagicMock()
    mock_service.files().create.return_value = mock_create
    mock_create.execute.return_value = {"id": "new_file_id"}
    def created_metadata():
        body = mock_service.files().create.call_args.kwargs["body"]
        return {
            "id": "new_file_id",
            "name": "rechnung.pdf",
            "parents": ["folder_id"],
            "md5Checksum": GoogleDriveClient._md5_file(local_file),
            "size": str(local_file.stat().st_size),
            "mimeType": "application/pdf",
            "trashed": False,
            "appProperties": body["appProperties"],
        }
    
    with tempfile.TemporaryDirectory() as tmpdir:
        local_file = Path(tmpdir) / "rechnung.pdf"
        local_file.write_text("dummy pdf", encoding="utf-8")
        mock_service.files().get.return_value.execute.side_effect = created_metadata
        
        client = GoogleDriveClient()
        file_id = client.upload_file("dummy_token.json", str(local_file), "Laura/Schule")
        
        assert file_id == "new_file_id"
        mock_service.files().create.assert_called_once()
        mock_service.files().update.assert_not_called()
        properties = mock_service.files().create.call_args.kwargs["body"]["appProperties"]
        assert properties["unifiedOcrRole"] == "pdf"
        assert properties["unifiedOcrMd5"] == GoogleDriveClient._md5_file(local_file)
        assert properties["unifiedOcrPackage"]

@patch("core.cloud.gdrive_client.MediaFileUpload")
@patch("core.cloud.gdrive_client.GoogleDriveClient._get_service")
@patch("core.cloud.gdrive_client.GoogleDriveClient._resolve_path_to_folder_id")
def test_upload_file_creates_safe_conflict_copy_for_existing_unknown_content(mock_resolve, mock_get_service, mock_media):
    mock_service = MagicMock()
    mock_get_service.return_value = mock_service
    mock_resolve.return_value = "folder_id"
    
    # Mock list: file exists with ID 'existing_file_id'
    mock_list = MagicMock()
    mock_service.files().list.return_value = mock_list
    mock_list.execute.side_effect = [
        {"files": [{"id": "existing_file_id", "name": "rechnung.pdf"}]},
        {"files": []},
        {"files": [{"id": "conflict_file_id", "name": "rechnung_conflict_001.pdf"}]},
    ]
    
    mock_create = MagicMock()
    mock_service.files().create.return_value = mock_create
    mock_create.execute.return_value = {"id": "conflict_file_id"}
    
    with tempfile.TemporaryDirectory() as tmpdir:
        local_file = Path(tmpdir) / "rechnung.pdf"
        local_file.write_text("dummy pdf", encoding="utf-8")
        def created_metadata():
            body = mock_service.files().create.call_args.kwargs["body"]
            return {
                "id": "conflict_file_id",
                "name": "rechnung_conflict_001.pdf",
                "parents": ["folder_id"],
                "md5Checksum": GoogleDriveClient._md5_file(local_file),
                "size": str(local_file.stat().st_size),
                "mimeType": "application/pdf",
                "trashed": False,
                "appProperties": body["appProperties"],
            }
        mock_service.files().get.return_value.execute.side_effect = created_metadata
        
        client = GoogleDriveClient()
        file_id = client.upload_file("dummy_token.json", str(local_file), "Laura/Schule")
        
        assert file_id == "conflict_file_id"
        mock_service.files().update.assert_not_called()
        metadata = mock_service.files().create.call_args.kwargs["body"]
        assert metadata["name"] == "rechnung_conflict_001.pdf"

@patch("core.cloud.gdrive_client.Path.exists")
@patch("core.cloud.gdrive_client.Path.unlink")
def test_logout(mock_unlink, mock_exists):
    mock_exists.return_value = True
    client = GoogleDriveClient()
    client.logout("dummy_token.json")
    mock_unlink.assert_called_once()


def test_ensure_folder_path_creates_and_returns_path_ids():
    mock_service = MagicMock()
    mock_list = MagicMock()
    mock_service.files().list.return_value = mock_list
    mock_list.execute.return_value = {"files": []}

    mock_create = MagicMock()
    mock_service.files().create.return_value = mock_create
    mock_create.execute.side_effect = [
        {"id": "jan_id"},
        {"id": "gesundheit_id"},
    ]

    client = GoogleDriveClient()
    result = client.ensure_folder_path(mock_service, "Jan/Gesundheit")

    assert result["folder_id"] == "gesundheit_id"
    assert result["created"] == ["Jan", "Jan/Gesundheit"]
    assert result["path_ids"] == {
        "Jan": "jan_id",
        "Jan/Gesundheit": "gesundheit_id",
    }


def test_ensure_folder_path_blocks_duplicate_conflict():
    mock_service = MagicMock()
    mock_list = MagicMock()
    mock_service.files().list.return_value = mock_list
    mock_list.execute.return_value = {
        "files": [
            {"id": "folder_1", "name": "Jan"},
            {"id": "folder_2", "name": "Jan"},
        ]
    }

    client = GoogleDriveClient()
    with pytest.raises(RuntimeError, match="Mehrdeutige.*Jan"):
        client.ensure_folder_path(mock_service, "Jan")


def test_stale_known_folder_id_blocks_instead_of_falling_back_by_name():
    mock_service = MagicMock()
    files_api = mock_service.files.return_value
    files_api.get.return_value.execute.side_effect = [
        {
            "id": "jan_id",
            "name": "Jan",
            "parents": ["root-id"],
            "trashed": False,
            "mimeType": "application/vnd.google-apps.folder",
        },
        {"id": "root-id"},
        {
            "id": "wrong_child",
            "name": "Gesundheit",
            "parents": ["other_parent"],
            "trashed": False,
            "mimeType": "application/vnd.google-apps.folder",
        },
    ]
    files_api.list.return_value.execute.return_value = {
        "files": [{"id": "correct_child", "name": "Gesundheit", "parents": ["jan_id"]}]
    }

    with pytest.raises(RuntimeError, match="geschlossen blockiert.*wrong_child"):
        GoogleDriveClient().ensure_folder_path(
            mock_service,
            "Jan/Gesundheit",
            known_ids={"Jan": "jan_id", "Jan/Gesundheit": "wrong_child"},
        )

    # The stale registry mapping is not silently replaced by the first
    # same-name folder returned from Drive.
    files_api.create.assert_not_called()


def test_missing_known_folder_id_blocks_before_name_lookup():
    service = MagicMock()
    files_api = service.files.return_value
    files_api.get.return_value.execute.side_effect = RuntimeError("404")

    with pytest.raises(RuntimeError, match="geschlossen blockiert.*gone-id"):
        GoogleDriveClient().ensure_folder_path(
            service,
            "Archiv",
            known_ids={"Archiv": "gone-id"},
        )

    files_api.list.assert_not_called()
    files_api.create.assert_not_called()


def test_known_top_level_folder_under_wrong_parent_is_blocked():
    service = MagicMock()
    files_api = service.files.return_value
    files_api.get.return_value.execute.side_effect = [
        {
            "id": "archive-id",
            "name": "Archiv",
            "parents": ["foreign-parent"],
            "trashed": False,
            "mimeType": "application/vnd.google-apps.folder",
        },
        {"id": "actual-root-id"},
    ]

    with pytest.raises(RuntimeError, match="geschlossen blockiert.*archive-id"):
        GoogleDriveClient().ensure_folder_path(
            service,
            "Archiv",
            known_ids={"Archiv": "archive-id"},
        )

    files_api.list.assert_not_called()
