from pathlib import Path

from core.release_check import format_release_check, run_release_check


def _write_minimal_release_tree(root: Path):
    (root / "unified_ocr_app").mkdir()
    (root / ".github" / "workflows").mkdir(parents=True)
    for path in [
        root / ".github" / "workflows" / "ci.yml",
        root / "README.md",
        root / "LICENSE",
        root / "unified_ocr_app" / "SECURITY.md",
        root / "unified_ocr_app" / "THIRD_PARTY_LICENSES.md",
    ]:
        path.write_text("ok", encoding="utf-8")
    (root / "unified_ocr_app" / "__init__.py").write_text('__version__ = "1.2.3"\n', encoding="utf-8")
    (root / "unified_ocr_app" / "pyproject.toml").write_text(
        '\n'.join([
            "[project]",
            'version = "1.2.3"',
            "dependencies = [",
            '    "requests>=2.31",',
            "]",
            "",
            "[project.optional-dependencies]",
            "dev = [",
            '    "pytest>=8.0",',
            "]",
            "",
            "[tool.setuptools]",
            'py-modules = ["app"]',
        ]),
        encoding="utf-8",
    )
    (root / "unified_ocr_app" / "app.py").write_text("def main(): pass\n", encoding="utf-8")
    requirements = "requests>=2.31\npytest>=8.0\n"
    (root / "requirements.txt").write_text(requirements, encoding="utf-8")
    (root / "unified_ocr_app" / "requirements.txt").write_text(requirements, encoding="utf-8")

    ignore_lines = [
        ".env",
        ".env.*",
        "*.key",
        "*.pem",
        "credentials.json",
        "token.json",
        "google_drive_token.json",
        "client_secret*.json",
        "llm_config.yaml",
        "settings.json",
        "settings.json.bak",
        "folder_registry.json",
        "folder_registry.backup.json",
        "classification_memory.json",
        "unified_ocr.sqlite3",
        "*.sqlite3",
        "cache.db",
        "*.log",
        "*.tmp",
    ]
    (root / ".gitignore").write_text("\n".join(ignore_lines), encoding="utf-8")


def _check(result: dict, name: str) -> dict:
    return next(check for check in result["checks"] if check["name"] == name)


def test_release_check_passes_minimal_clean_tree(tmp_path):
    _write_minimal_release_tree(tmp_path)

    result = run_release_check(tmp_path)

    assert result["status"] == "ok"
    assert all(check["status"] == "ok" for check in result["checks"])


def test_release_check_flags_sensitive_files_and_secret_markers(tmp_path):
    _write_minimal_release_tree(tmp_path)
    (tmp_path / "credentials.json").write_text("{}", encoding="utf-8")
    (tmp_path / "settings.json.bak").write_text("{}", encoding="utf-8")
    (tmp_path / "settings.json.tmp").write_text("{}", encoding="utf-8")
    (tmp_path / "google_drive_token.json").write_text("{}", encoding="utf-8")
    (tmp_path / "runtime.sqlite3").write_text("", encoding="utf-8")
    (tmp_path / "unified_ocr_app" / "leak.py").write_text(
        'api_key = "sk-' + ("a" * 32) + '"\n',
        encoding="utf-8",
    )

    result = run_release_check(tmp_path)

    assert result["status"] == "failed"
    assert _check(result, "sensitive_files")["status"] == "failed"
    assert _check(result, "secret_markers")["status"] == "failed"
    sensitive_details = "\n".join(_check(result, "sensitive_files")["details"])
    assert "credentials.json" in sensitive_details
    assert "settings.json.bak" in sensitive_details
    assert "settings.json.tmp" in sensitive_details
    assert "google_drive_token.json" in sensitive_details
    assert "runtime.sqlite3" in sensitive_details


def test_format_release_check_lists_check_details(tmp_path):
    _write_minimal_release_tree(tmp_path)
    (tmp_path / ".gitignore").write_text(".env\n", encoding="utf-8")

    rendered = format_release_check(run_release_check(tmp_path))

    assert "Release-Check: WARNING" in rendered
    assert "gitignore_rules" in rendered
    assert "credentials.json" in rendered


def test_release_check_flags_dependency_metadata_drift(tmp_path):
    _write_minimal_release_tree(tmp_path)
    (tmp_path / "unified_ocr_app" / "__init__.py").write_text('__version__ = "9.9.9"\n', encoding="utf-8")
    (tmp_path / "requirements.txt").write_text("requests>=2.31\n", encoding="utf-8")

    result = run_release_check(tmp_path)
    details = "\n".join(_check(result, "dependency_metadata")["details"])

    assert result["status"] == "failed"
    assert "Version mismatch" in details
    assert "pytest>=8.0" in details


def test_release_check_flags_missing_py_module(tmp_path):
    _write_minimal_release_tree(tmp_path)
    pyproject = (tmp_path / "unified_ocr_app" / "pyproject.toml").read_text(encoding="utf-8")
    (tmp_path / "unified_ocr_app" / "pyproject.toml").write_text(
        pyproject.replace('py-modules = ["app"]', 'py-modules = ["app", "missing_module"]'),
        encoding="utf-8",
    )

    result = run_release_check(tmp_path)
    details = "\n".join(_check(result, "dependency_metadata")["details"])

    assert result["status"] == "failed"
    assert "missing_module.py" in details


def test_ci_workflow_runs_tests_and_release_check():
    workflow = (Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert "python-version: \"3.10\"" in workflow
    assert "python -m pytest unified_ocr_app" in workflow
    assert "python unified_ocr_app/release_check.py" in workflow
