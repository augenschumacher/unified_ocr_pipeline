import json

from core.settings import SettingsManager


def test_unsafe_medical_defaults_are_migrated_without_today_fallback(tmp_path):
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps(
            {
                "base_dir": str(tmp_path / "archive"),
                "prompt_version": 1,
                "prompts": {
                    "vision": "Du bist ein medizinischer OCR-Korrektor. Korrigiere Fehler, ergänze Fehlendes.",
                    "fusion": "Erstelle einen fehlerfreien, flüssigen Fließtext. STANDARD-AUSGABE IST DEUTSCH.",
                    "analysis": "Du bist ein medizinischer Archivar. Datum aus dem Dokument, sonst das heutige.",
                    "image_description": "Eigene Bildbeschreibung",
                },
            }
        ),
        encoding="utf-8",
    )

    manager = SettingsManager(settings_path)
    loaded = manager.settings

    assert loaded["prompt_version"] == SettingsManager.CURRENT_PROMPT_VERSION
    assert "medizinisch" not in loaded["prompts"]["vision"].casefold()
    assert "ergänze fehlendes" not in loaded["prompts"]["vision"].casefold()
    assert "standard-ausgabe ist deutsch" not in loaded["prompts"]["fusion"].casefold()
    assert "sonst das heutige" not in loaded["prompts"]["analysis"].casefold()
    assert "niemals" in loaded["prompts"]["analysis"].casefold()
    assert "null" in loaded["prompts"]["analysis"].casefold()
    assert loaded["prompts"]["image_description"] == "Eigene Bildbeschreibung"


def test_safe_custom_prompts_survive_version_migration(tmp_path):
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps(
            {
                "base_dir": str(tmp_path / "archive"),
                "prompt_version": 2,
                "prompts": {
                    "vision": "Mein strenger bildtreuer OCR-Prompt",
                    "fusion": "Mein quellennaher Fusionsprompt",
                    "analysis": "Mein JSON-Metadatenprompt ohne Ersatzwerte",
                    "image_description": "Meine Bildbeschreibung",
                },
            }
        ),
        encoding="utf-8",
    )

    loaded = SettingsManager(settings_path).settings

    assert loaded["prompts"]["vision"] == "Mein strenger bildtreuer OCR-Prompt"
    assert loaded["prompts"]["fusion"] == "Mein quellennaher Fusionsprompt"
    assert loaded["prompts"]["analysis"] == "Mein JSON-Metadatenprompt ohne Ersatzwerte"
    assert loaded["prompt_version"] == SettingsManager.CURRENT_PROMPT_VERSION
