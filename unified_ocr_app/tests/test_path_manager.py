from ui.path_manager import remap_context_paths, rename_child_node


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
