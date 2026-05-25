from unittest.mock import MagicMock

from core.cloud.drive_sync import build_drive_sync_preview, sync_drive_folders
from core.cloud.folder_registry import FolderRegistry


def test_drive_sync_preview_counts_missing_ids(tmp_path):
    registry = FolderRegistry(tmp_path)
    registry.save_tree({"Fabio": {"Gesundheit": {}}})
    registry.set_drive_folder_id("Fabio", "fabio_id")
    registry.save()

    preview = build_drive_sync_preview(registry)

    assert preview["total"] == 2
    assert preview["mapped"] == ["Fabio"]
    assert preview["missing_ids"] == ["Fabio/Gesundheit"]


def test_sync_drive_folders_persists_ids(tmp_path):
    registry = FolderRegistry(tmp_path)
    registry.save_tree({"Fabio": {"Gesundheit": {}}})

    client = MagicMock()
    service = MagicMock()
    client._get_service.return_value = service
    client.ensure_folder_path.side_effect = [
        {
            "folder_id": "fabio_id",
            "created": ["Fabio"],
            "found": [],
            "conflicts": [],
            "path_ids": {"Fabio": "fabio_id"},
        },
        {
            "folder_id": "gesundheit_id",
            "created": ["Fabio/Gesundheit"],
            "found": ["Fabio"],
            "conflicts": [],
            "path_ids": {"Fabio": "fabio_id", "Fabio/Gesundheit": "gesundheit_id"},
        },
    ]

    result = sync_drive_folders(tmp_path, "token.json", client=client)
    reloaded = FolderRegistry(tmp_path)

    assert result["created"] == ["Fabio", "Fabio/Gesundheit"]
    assert reloaded.get_drive_folder_id("Fabio") == "fabio_id"
    assert reloaded.get_drive_folder_id("Fabio/Gesundheit") == "gesundheit_id"
