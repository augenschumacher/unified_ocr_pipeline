import importlib.util
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]


def _load_installer_module():
    installer_path = ROOT / "packaging" / "windows" / "installer.py"
    spec = importlib.util.spec_from_file_location("unified_ocr_installer", installer_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_model_recommendations_load_from_json_catalog():
    from core.model_recommendations import load_recommendations

    catalog = ROOT / "unified_ocr_app" / "resources" / "ollama_model_recommendations.json"
    recommendations = load_recommendations(catalog)

    assert [item.vram_gb for item in recommendations] == [8, 12, 16, 24, 32]
    assert recommendations[0].vision == "qwen3-vl:4b-instruct-q4_K_M"
    assert recommendations[-1].required_free_gb >= recommendations[-1].estimated_download_gb


def test_installer_loads_model_catalog_and_preflight_shape(tmp_path):
    installer = _load_installer_module()
    payload = tmp_path / "payload.zip"
    payload.write_bytes(b"zip")

    with patch.object(installer, "find_command", return_value=None), \
         patch.object(installer, "winget_available", return_value=True), \
         patch.object(installer, "ollama_command", return_value=None), \
         patch.object(installer, "free_space_gb", return_value=123.4):
        result = installer.installer_preflight(str(payload))

    assert result["payload"]["exists"] is True
    assert result["dependencies"]["winget_available"] is True
    assert result["ollama"]["recommendations"]["8"]["models"][0] == "glm-ocr:bf16"
    assert result["ollama"]["models_dir_free_gb"] == 123.4


def test_installer_detects_unsafe_delete_target(tmp_path):
    installer = _load_installer_module()
    outside = tmp_path / "outside"
    allowed = tmp_path / "allowed"
    outside.mkdir()
    allowed.mkdir()

    try:
        installer.remove_directory_retry(outside, allowed)
    except RuntimeError as exc:
        assert "Unsicherer Loeschpfad" in str(exc)
    else:
        raise AssertionError("unsafe delete target was not rejected")


def test_installer_detects_winget_from_windowsapps_alias(tmp_path, monkeypatch):
    installer = _load_installer_module()
    windows_apps = tmp_path / "Microsoft" / "WindowsApps"
    windows_apps.mkdir(parents=True)
    (windows_apps / "winget.exe").write_text("", encoding="utf-8")

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("PATH", "")

    assert installer.winget_available() is True


def test_pyinstaller_spec_bundles_drag_and_drop_runtime():
    spec_text = (ROOT / "packaging" / "windows" / "UnifiedOCR.spec").read_text(encoding="utf-8")

    assert 'collect_data_files("tkinterdnd2")' in spec_text
    assert '"tkinterdnd2"' in spec_text
