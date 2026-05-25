from ui.path_manager import (
    apply_standard_template,
    create_final_directories,
    validate_folder_name,
)


def test_validate_folder_name_rejects_windows_invalid_names():
    for name in ["", "A/B", "A\\B", "A:B", "CON", "NUL", "Name.", "Name "]:
        ok, _ = validate_folder_name(name)
        assert not ok


def test_validate_folder_name_accepts_person_name():
    ok, _ = validate_folder_name("Jan")
    assert ok


def test_standard_template_uses_person_as_primary_path():
    tree = {}
    added, rejected = apply_standard_template(tree, ["Jan"])

    assert rejected == []
    assert added > 1
    assert "Jan" in tree
    assert "Gesundheit" in tree["Jan"]
    assert "Finanzen" in tree["Jan"]


def test_standard_template_prevents_case_insensitive_duplicates():
    tree = {"Jan": {"Gesundheit": {}}}
    apply_standard_template(tree, ["jan"])

    assert sorted(tree.keys()) == ["Jan"]
    assert "Gesundheit" in tree["Jan"]


def test_create_final_directories(tmp_path):
    tree = {"Jan": {"Gesundheit": {}, "Finanzen": {}}}
    created = create_final_directories(tmp_path, tree)

    assert tmp_path.joinpath("final", "Jan").is_dir()
    assert tmp_path.joinpath("final", "Jan", "Gesundheit").is_dir()
    assert tmp_path.joinpath("final", "Jan", "Finanzen").is_dir()
    assert len(created) == 3
