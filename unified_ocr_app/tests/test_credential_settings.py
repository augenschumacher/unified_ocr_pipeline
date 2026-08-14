import json
from unittest.mock import patch

from core.credential_store import make_secret_ref
from core.settings import SettingsManager


def test_synology_password_is_saved_as_credential_reference(tmp_path):
    settings_path = tmp_path / "settings.json"
    ref = make_secret_ref("synology_webdav_password")

    with patch("core.settings.store_secret", return_value=ref):
        manager = SettingsManager(settings_path)
        data = manager.settings.copy()
        data["synology_enabled"] = True
        data["synology_base_url"] = "https://nas.local:5006"
        data["synology_username"] = "ocr"
        data["synology_password"] = "very-secret"
        manager.save(data)

    raw = json.loads(settings_path.read_text(encoding="utf-8"))

    assert raw["synology_password"] == ref
    assert raw["synology_password_storage"] == "credential_manager"
    assert "very-secret" not in settings_path.read_text(encoding="utf-8")


def test_synology_password_reference_is_resolved_on_load(tmp_path):
    settings_path = tmp_path / "settings.json"
    ref = make_secret_ref("synology_webdav_password")
    settings_path.write_text(json.dumps({
        "base_dir": str(tmp_path),
        "output_format": "PDF und DOCX",
        "docx_mode": "Lesbare DOCX",
        "models": {
            "vision": "Keins",
            "fusion": "Keins",
            "analysis": "Keins",
            "glm_ocr": "Keins",
        },
        "synology_password": ref,
    }), encoding="utf-8")

    with patch("core.settings.load_secret", return_value="resolved-secret"):
        manager = SettingsManager(settings_path)

    assert manager.settings["synology_password"] == "resolved-secret"


def test_settings_backup_scrubs_legacy_plaintext_password(tmp_path):
    settings_path = tmp_path / "settings.json"
    ref = make_secret_ref("synology_webdav_password")
    legacy_data = {
        "base_dir": str(tmp_path),
        "output_format": "PDF und DOCX",
        "docx_mode": "Lesbare DOCX",
        "models": {
            "vision": "Keins",
            "fusion": "Keins",
            "analysis": "Keins",
            "glm_ocr": "Keins",
        },
        "synology_password": "old-secret",
    }
    settings_path.write_text(json.dumps(legacy_data), encoding="utf-8")

    with patch("core.settings.store_secret", return_value=ref):
        manager = SettingsManager(settings_path)
        data = manager.settings.copy()
        data["synology_password"] = "new-secret"
        manager.save(data)

    backup_text = manager.backup_path().read_text(encoding="utf-8")
    current_text = settings_path.read_text(encoding="utf-8")

    assert "old-secret" not in backup_text
    assert "new-secret" not in current_text
    assert json.loads(manager.backup_path().read_text(encoding="utf-8"))["synology_password"] == ref
