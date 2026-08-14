import pytest

from core.cloud.folder_registry import FolderRegistry, RegistryWriteError, UnsafeArchivePath
from ui.path_manager import (
    archive_directory_has_records,
    remap_context_paths,
    rename_child_node,
    validate_folder_name,
)


def test_rename_child_node_preserves_subtree():
    node = {"Auto": {"Golf": {}}, "Gesundheit": {}}

    ok, new_name = rename_child_node(node, "Auto", "Fahrzeuge")

    assert ok
    assert new_name == "Fahrzeuge"
    assert "Auto" not in node
    assert node["Fahrzeuge"] == {"Golf": {}}
    assert "Gesundheit" in node


def test_rename_child_node_rejects_duplicate_case_insensitive():
    node = {"Auto": {}, "Gesundheit": {}}

    ok, message = rename_child_node(node, "Auto", "gesundheit")

    assert not ok
    assert "existiert bereits" in message
    assert set(node) == {"Auto", "Gesundheit"}


def test_remap_context_paths_updates_descendants():
    contexts = {
        "Fabio/Auto": {"notes": "root"},
        "Fabio/Auto/Golf": {"notes": "child"},
        "Fabio/Gesundheit": {"notes": "other"},
    }

    remapped = remap_context_paths(contexts, "Fabio/Auto", "Fabio/Fahrzeuge")

    assert "Fabio/Auto" not in remapped
    assert "Fabio/Auto/Golf" not in remapped
    assert remapped["Fabio/Fahrzeuge"] == {"notes": "root"}
    assert remapped["Fabio/Fahrzeuge/Golf"] == {"notes": "child"}
    assert remapped["Fabio/Gesundheit"] == {"notes": "other"}


def test_archive_directory_with_records_is_not_safe_for_incidental_rename(tmp_path):
    final_dir = tmp_path / "final"
    empty_path = final_dir / "Fabio" / "Neu"
    occupied_path = final_dir / "Fabio" / "Auto"
    empty_path.mkdir(parents=True)
    occupied_path.mkdir(parents=True)
    (occupied_path / "rechnung.pdf").write_bytes(b"pdf")

    assert archive_directory_has_records(final_dir, "Fabio/Neu") is False
    assert archive_directory_has_records(final_dir, "Fabio/Auto") is True


@pytest.mark.parametrize("name", ["_staging", "_transactions", "_recovery", "begleitdateien"])
def test_internal_archive_names_cannot_be_registered(name, tmp_path):
    ok, message = validate_folder_name(name)
    assert ok is False
    assert "reserviert" in message

    registry = FolderRegistry(tmp_path)
    assert registry.add_person(name) is False
    with pytest.raises(UnsafeArchivePath, match="interne Programmdaten"):
        registry.save_tree({name: {}})


def test_populated_archive_path_cannot_disappear_from_registry(tmp_path):
    registry = FolderRegistry(tmp_path)
    assert registry.add_person("Fabio") is True
    assert registry.add_path("Fabio/Auto") is True
    occupied = tmp_path / "final" / "Fabio" / "Auto"
    occupied.mkdir(parents=True)
    (occupied / "vertrag.pdf").write_bytes(b"pdf")

    with pytest.raises(RegistryWriteError, match="Belegter Archivpfad"):
        registry.save_tree({"Fabio": {}})

    assert "Fabio/Auto" in FolderRegistry(tmp_path).get_known_paths()


def test_stale_registry_instance_cannot_overwrite_newer_paths(tmp_path):
    first = FolderRegistry(tmp_path)
    assert first.add_person("Fabio") is True
    stale = FolderRegistry(tmp_path)

    assert first.add_path("Fabio/Finanzen") is True
    with pytest.raises(RegistryWriteError, match="zwischenzeitlich geändert"):
        stale.add_path("Fabio/Verträge")

    current = FolderRegistry(tmp_path)
    assert "Fabio/Finanzen" in current.get_known_paths()
    assert "Fabio/Verträge" not in current.get_known_paths()
