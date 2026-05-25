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
    # 1. Natalia exists under root -> ID 'natalia_id'
    # 2. Schule exists under 'natalia_id' -> ID 'schule_id'
    mock_list = MagicMock()
    mock_service.files().list.return_value = mock_list
    
    mock_list.execute.side_effect = [
        {"files": [{"id": "natalia_id", "name": "Natalia"}]},
        {"files": [{"id": "schule_id", "name": "Schule"}]}
    ]
    
    client = GoogleDriveClient()
    folder_id = client._resolve_path_to_folder_id(mock_service, "Natalia/Schule")
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
        {"id": "new_natalia_id"},
        {"id": "new_schule_id"}
    ]
    
    client = GoogleDriveClient()
    folder_id = client._resolve_path_to_folder_id(mock_service, "Natalia/Schule")
    
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
    mock_list.execute.return_value = {"files": []}
    
    # Mock create
    mock_create = MagicMock()
    mock_service.files().create.return_value = mock_create
    mock_create.execute.return_value = {"id": "new_file_id"}
    
    with tempfile.TemporaryDirectory() as tmpdir:
        local_file = Path(tmpdir) / "rechnung.pdf"
        local_file.write_text("dummy pdf", encoding="utf-8")
        
        client = GoogleDriveClient()
        file_id = client.upload_file("dummy_token.json", str(local_file), "Natalia/Schule")
        
        assert file_id == "new_file_id"
        mock_service.files().create.assert_called_once()
        mock_service.files().update.assert_not_called()

@patch("core.cloud.gdrive_client.MediaFileUpload")
@patch("core.cloud.gdrive_client.GoogleDriveClient._get_service")
@patch("core.cloud.gdrive_client.GoogleDriveClient._resolve_path_to_folder_id")
def test_upload_file_update_existing(mock_resolve, mock_get_service, mock_media):
    mock_service = MagicMock()
    mock_get_service.return_value = mock_service
    mock_resolve.return_value = "folder_id"
    
    # Mock list: file exists with ID 'existing_file_id'
    mock_list = MagicMock()
    mock_service.files().list.return_value = mock_list
    mock_list.execute.return_value = {"files": [{"id": "existing_file_id", "name": "rechnung.pdf"}]}
    
    # Mock update
    mock_update = MagicMock()
    mock_service.files().update.return_value = mock_update
    mock_update.execute.return_value = {"id": "existing_file_id"}
    
    with tempfile.TemporaryDirectory() as tmpdir:
        local_file = Path(tmpdir) / "rechnung.pdf"
        local_file.write_text("dummy pdf", encoding="utf-8")
        
        client = GoogleDriveClient()
        file_id = client.upload_file("dummy_token.json", str(local_file), "Natalia/Schule")
        
        assert file_id == "existing_file_id"
        mock_service.files().create.assert_not_called()
        mock_service.files().update.assert_called_once()

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
        {"id": "fabio_id"},
        {"id": "gesundheit_id"},
    ]

    client = GoogleDriveClient()
    result = client.ensure_folder_path(mock_service, "Fabio/Gesundheit")

    assert result["folder_id"] == "gesundheit_id"
    assert result["created"] == ["Fabio", "Fabio/Gesundheit"]
    assert result["path_ids"] == {
        "Fabio": "fabio_id",
        "Fabio/Gesundheit": "gesundheit_id",
    }


def test_ensure_folder_path_reports_duplicate_conflict():
    mock_service = MagicMock()
    mock_list = MagicMock()
    mock_service.files().list.return_value = mock_list
    mock_list.execute.return_value = {
        "files": [
            {"id": "folder_1", "name": "Fabio"},
            {"id": "folder_2", "name": "Fabio"},
        ]
    }

    client = GoogleDriveClient()
    result = client.ensure_folder_path(mock_service, "Fabio")

    assert result["folder_id"] == "folder_1"
    assert result["conflicts"]
    assert result["conflicts"][0]["folder_ids"] == ["folder_1", "folder_2"]
