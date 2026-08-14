from __future__ import annotations

from pathlib import Path

from core.cloud.folder_registry import FolderRegistry, RegistryWriteError
from core.cloud.gdrive_client import GoogleDriveClient


def build_drive_sync_preview(registry: FolderRegistry) -> dict:
    known_paths = registry.get_known_paths()
    drive_map = registry.get_drive_folder_map()
    missing_ids = [path for path in known_paths if path not in drive_map]
    mapped = [path for path in known_paths if path in drive_map]
    return {
        "total": len(known_paths),
        "missing_ids": missing_ids,
        "mapped": mapped,
    }


def sync_drive_folders(base_dir: Path, token_path: str, client: GoogleDriveClient | None = None) -> dict:
    """
    Ensure all registry paths exist in Google Drive and persist their folder IDs.
    This never deletes Drive folders. It creates missing paths and reuses existing
    folders by name where no ID is known yet.
    """
    registry = FolderRegistry(base_dir)
    client = client or GoogleDriveClient()
    service = client._get_service(token_path)
    if not service:
        raise ValueError("Keine gültige Verbindung zu Google Drive vorhanden.")

    known_ids = registry.get_drive_folder_map()
    created = []
    found = []
    conflicts = []

    for path in sorted(registry.get_known_paths(), key=lambda p: (p.count("/"), p.casefold())):
        result = client.ensure_folder_path(service, path, known_ids=known_ids)
        for p, folder_id in result["path_ids"].items():
            registry.set_drive_folder_id(p, folder_id)
            known_ids[p] = folder_id
        created.extend(result["created"])
        found.extend(result["found"])
        conflicts.extend(result["conflicts"])

    registry.prune_drive_folder_map()
    try:
        registry.save()
    except RegistryWriteError as exc:
        if "zwischenzeitlich geändert" not in str(exc):
            raise
        # Merge only Drive IDs into the newest tree; a concurrently added local
        # archive path must never disappear because this sync held a stale copy.
        fresh = FolderRegistry(base_dir)
        current_paths = set(fresh.get_known_paths())
        for path, folder_id in registry.get_drive_folder_map().items():
            if path in current_paths:
                fresh.set_drive_folder_id(path, folder_id)
        fresh.prune_drive_folder_map()
        fresh.save()
        registry = fresh

    return {
        "created": sorted(set(created), key=lambda p: (p.count("/"), p.casefold())),
        "found": sorted(set(found), key=lambda p: (p.count("/"), p.casefold())),
        "conflicts": conflicts,
        "mapped": registry.get_drive_folder_map(),
    }
