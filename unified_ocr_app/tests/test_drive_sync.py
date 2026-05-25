from unittest.mock import MagicMock

from core.cloud.drive_sync import build_drive_sync_preview, sync_drive_folders
from core.cloud.folder_registry import FolderRegistry


def test_drive_sync_preview_counts_missing_ids(tmp_path):
    registry = FolderRegistry(tmp_path)
    registry.save_tree({"Jan": {"Gesundheit": {}}})
    registry.set_drive_folder_id("Jan", "jan_id")
    registry.save()

    preview = build_drive_sync_preview(registry)

    assert preview["total"] == 2
    assert preview["mapped"] == ["Jan"]
    assert preview["missing_ids"] == ["Jan/Gesundheit"]


def test_sync_drive_folders_persists_ids(tmp_path):
    registry = FolderRegistry(tmp_path)
    registry.save_tree({"Jan": {"Gesundheit": {}}})

    client = MagicMock()
    service = MagicMock()
    client._get_service.return_value = service
    client.ensure_folder_path.side_effect = [
        {
            "folder_id": "jan_id",
            "created": ["Jan"],
            "found": [],
            "conflicts": [],
            "path_ids": {"Jan": "jan_id"},
        },
        {
            "folder_id": "gesundheit_id",
            "created": ["Jan/Gesundheit"],
            "found": ["Jan"],
            "conflicts": [],
            "path_ids": {"Jan": "jan_id", "Jan/Gesundheit": "gesundheit_id"},
        },
    ]

    result = sync_drive_folders(tmp_path, "token.json", client=client)
    reloaded = FolderRegistry(tmp_path)

    assert result["created"] == ["Jan", "Jan/Gesundheit"]
    assert reloaded.get_drive_folder_id("Jan") == "jan_id"
    assert reloaded.get_drive_folder_id("Jan/Gesundheit") == "gesundheit_id"
