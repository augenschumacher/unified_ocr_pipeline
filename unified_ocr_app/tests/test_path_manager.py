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
    ok, _ = validate_folder_name("Fabio")
    assert ok


def test_standard_template_uses_person_as_primary_path():
    tree = {}
    added, rejected = apply_standard_template(tree, ["Fabio"])

    assert rejected == []
    assert added > 1
    assert "Fabio" in tree
    assert "Gesundheit" in tree["Fabio"]
    assert "Finanzen" in tree["Fabio"]


def test_standard_template_prevents_case_insensitive_duplicates():
    tree = {"Fabio": {"Gesundheit": {}}}
    apply_standard_template(tree, ["fabio"])

    assert sorted(tree.keys()) == ["Fabio"]
    assert "Gesundheit" in tree["Fabio"]


def test_create_final_directories(tmp_path):
    tree = {"Fabio": {"Gesundheit": {}, "Finanzen": {}}}
    created = create_final_directories(tmp_path, tree)

    assert tmp_path.joinpath("final", "Fabio").is_dir()
    assert tmp_path.joinpath("final", "Fabio", "Gesundheit").is_dir()
    assert tmp_path.joinpath("final", "Fabio", "Finanzen").is_dir()
    assert len(created) == 3
