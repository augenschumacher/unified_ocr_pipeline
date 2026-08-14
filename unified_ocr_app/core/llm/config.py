from __future__ import annotations

import copy
import logging
import os
from pathlib import Path

import yaml

from core.credential_store import is_secret_ref, load_secret, store_secret
from core.model_recommendations import default_recommendation
from core.runtime_paths import get_user_data_dir, harden_private_file


logger = logging.getLogger("UnifiedOCR")

PROVIDER_SECRET_NAMES = {
    "openai": "llm_openai_api_key",
    "google": "llm_google_api_key",
    "mistral": "llm_mistral_api_key",
}
PROVIDER_ENV_KEYS = {
    "openai": ("OPENAI_API_KEY",),
    "google": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "mistral": ("MISTRAL_API_KEY",),
}


def default_llm_config_path() -> Path:
    return get_user_data_dir() / "llm_config.yaml"


def _default_config() -> dict:
    # Environment variables are deliberately not copied into the on-disk
    # defaults.  They are runtime credentials, not configuration content.
    return {
        "schema_version": 2,
        "providers": {
            "ollama": {"api_base": "http://localhost:11434"},
            "openai": {"api_key": ""},
            "google": {"api_key": ""},
            "mistral": {"api_key": ""},
        },
        "stages": default_recommendation().as_llm_config_stages(),
    }


def _deep_merge(base: dict, loaded: dict) -> dict:
    result = copy.deepcopy(base)
    if isinstance(loaded.get("providers"), dict):
        for provider, provider_data in loaded["providers"].items():
            if isinstance(provider_data, dict):
                result["providers"].setdefault(provider, {}).update(provider_data)
    if isinstance(loaded.get("stages"), dict):
        result["stages"].update(loaded["stages"])
    result["schema_version"] = max(2, int(loaded.get("schema_version") or 1))
    return result


def _environment_secret(provider: str) -> str:
    for name in PROVIDER_ENV_KEYS.get(provider, ()):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def _write_yaml_atomic(config_path: Path, data: dict) -> None:
    config_path = Path(config_path)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = config_path.with_name(f".{config_path.name}.tmp")
    try:
        temporary.write_text(
            yaml.safe_dump(data, default_flow_style=False, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
        harden_private_file(temporary)
        os.replace(temporary, config_path)
        harden_private_file(config_path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _config_for_disk(config: dict) -> dict:
    disk = copy.deepcopy(config)
    disk["schema_version"] = 2
    providers = disk.setdefault("providers", {})
    for provider, secret_name in PROVIDER_SECRET_NAMES.items():
        provider_config = providers.setdefault(provider, {})
        value = str(provider_config.get("api_key") or "").strip()
        if not value or is_secret_ref(value):
            continue
        reference = store_secret(secret_name, value)
        if reference is not None:
            provider_config["api_key"] = reference
    return disk


def save_llm_config(config: dict, config_path: Path | None = None) -> Path:
    path = Path(config_path or default_llm_config_path())
    _write_yaml_atomic(path, _config_for_disk(config))
    return path


def load_llm_config(config_path: Path | None = None) -> dict:
    """Load provider configuration and resolve secret references in memory."""
    path = Path(config_path or default_llm_config_path())
    disk_config = _default_config()
    migrated = False

    if path.exists():
        try:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            if isinstance(loaded, dict):
                disk_config = _deep_merge(disk_config, loaded)
            logger.info("Zentrale LLM-Konfiguration geladen aus: %s", path)
        except Exception as exc:
            logger.error("Fehler beim Laden von llm_config.yaml: %s. Nutze Standardwerte.", exc)
    else:
        try:
            _write_yaml_atomic(path, disk_config)
        except Exception as exc:
            logger.error("Konnte Standard-LLM-Konfiguration nicht schreiben: %s", exc)

    # Transparently migrate legacy plaintext values to the credential manager.
    for provider, secret_name in PROVIDER_SECRET_NAMES.items():
        provider_config = disk_config.setdefault("providers", {}).setdefault(provider, {})
        stored_value = str(provider_config.get("api_key") or "").strip()
        if stored_value and not is_secret_ref(stored_value):
            reference = store_secret(secret_name, stored_value)
            if reference is not None:
                provider_config["api_key"] = reference
                migrated = True

    if migrated:
        try:
            _write_yaml_atomic(path, disk_config)
        except Exception as exc:
            logger.warning("API-Schlüssel konnten nicht in Secret-Referenzen migriert werden: %s", exc)

    resolved = copy.deepcopy(disk_config)
    for provider in PROVIDER_SECRET_NAMES:
        provider_config = resolved["providers"].setdefault(provider, {})
        stored_value = str(provider_config.get("api_key") or "").strip()
        secret = load_secret(stored_value) if is_secret_ref(stored_value) else stored_value
        provider_config["api_key"] = secret or _environment_secret(provider)
        provider_config["credential_configured"] = bool(provider_config["api_key"])
    return resolved
