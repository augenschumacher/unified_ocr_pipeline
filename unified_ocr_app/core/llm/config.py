import os
import logging
import yaml
from pathlib import Path
from core.runtime_paths import get_user_data_dir

logger = logging.getLogger("UnifiedOCR")

def default_llm_config_path() -> Path:
    """Gibt den Standardpfad für die LLM-Konfigurationsdatei zurück."""
    return get_user_data_dir() / "llm_config.yaml"

def load_llm_config(config_path: Path | None = None) -> dict:
    """
    Lädt die zentrale LLM-Konfiguration (YAML).
    Erstellt eine Standardkonfiguration, falls die Datei nicht existiert.
    """
    if config_path is None:
        config_path = default_llm_config_path()
    else:
        config_path = Path(config_path)

    default_config = {
        "providers": {
            "ollama": {
                "api_base": "http://localhost:11434"
            },
            "openai": {
                "api_key": os.environ.get("OPENAI_API_KEY", "")
            },
            "google": {
                "api_key": os.environ.get("GEMINI_API_KEY", os.environ.get("GOOGLE_API_KEY", ""))
            },
            "mistral": {
                "api_key": os.environ.get("MISTRAL_API_KEY", "")
            }
        },
        "stages": {
            "glm_ocr": "ollama/glm-ocr:bf16",
            "vision": "ollama/qwen3-vl:30b-a3b-instruct-q4_K_M",
            "fusion": "ollama/qwen3.6:27b",
            "analysis": "ollama/qwen3.6:27b"
        }
    }

    if config_path.exists():
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                loaded = yaml.safe_load(f)
                if loaded and isinstance(loaded, dict):
                    # Deep-Merge der geladenen Konfiguration mit den Defaults
                    if "providers" in loaded and isinstance(loaded["providers"], dict):
                        for prov, prov_data in loaded["providers"].items():
                            if isinstance(prov_data, dict):
                                default_config["providers"].setdefault(prov, {}).update(prov_data)
                    if "stages" in loaded and isinstance(loaded["stages"], dict):
                        default_config["stages"].update(loaded["stages"])
            logger.info(f"Zentrale LLM-Konfiguration erfolgreich geladen aus: {config_path}")
        except Exception as e:
            logger.error(f"Fehler beim Laden von llm_config.yaml: {e}. Nutze Standardwerte.")
    else:
        # Standardkonfiguration schreiben
        try:
            config_path.parent.mkdir(parents=True, exist_ok=True)
            with open(config_path, "w", encoding="utf-8") as f:
                yaml.safe_dump(default_config, f, default_flow_style=False, sort_keys=False)
            logger.info(f"Standard-LLM-Konfiguration erstellt unter: {config_path}")
        except Exception as e:
            logger.error(f"Konnte Standard-LLM-Konfigurationsdatei nicht schreiben: {e}")

    return default_config
