import json
import logging
import os
import shutil
from pathlib import Path

from core.runtime_paths import (
    default_settings_path,
    legacy_settings_path,
    normalize_token_path,
    default_credentials_path,
    normalize_credentials_path,
    harden_private_file,
)
from core.credential_store import (
    SYNOLOGY_PASSWORD_NAME,
    delete_secret,
    is_secret_ref,
    load_secret,
    store_secret,
)
from core.model_recommendations import default_recommendation
from core.ocr.pdf_prep import normalize_ocr_languages, normalize_ocr_mode


logger = logging.getLogger("UnifiedOCR")


class SettingsManager:
    CURRENT_PROMPT_VERSION = 3

    def __init__(self, settings_file: str | Path | None = None):
        self.uses_default_location = settings_file is None
        self.settings_file = Path(settings_file) if settings_file is not None else default_settings_path()
        self.default_prompts = {
            "vision": (
                "Du bist ein präziser, domain-neutraler OCR-Korrektor und Layout-Analyst. "
                "Prüfe Lesereihenfolge, Tabellen, Absätze und Formatierungen kritisch am Seitenbild. "
                "Korrigiere nur bildlich belegte Fehler und ergänze nur tatsächlich Sichtbares. "
                "Erfinde oder glätte keine Namen, Zahlen, Daten, Beträge, Codes oder Aussagen. "
                "Umschließe erkannte Tabellen mit <table_block> und </table_block>. "
                "Gib nur das korrigierte Markdown zurück."
            ),
            "fusion": (
                "Du verarbeitest beliebige Dokumentarten wort- und beleggetreu. "
                "Führe Vision-Markdown, OCR-Sidecar und optionale OCR-Quellen für genau eine Seite zusammen. "
                "Bewahre Dokumentensprache, Lesereihenfolge, Namen, Zahlen, Daten, Beträge und Codes exakt. "
                "Erfinde nichts und markiere nicht auflösbare Widersprüche als [UNSICHER]. "
                "Nutze vorherigen Seitenkontext nur für echte Satzübergänge und wiederhole ihn nicht. "
                "Blöcke <table_block>...</table_block> bleiben unverändert. "
                "Gib nur den finalen Seitentext zurück."
            ),
            "analysis": (
                "Du bist ein professioneller, domain-neutraler Dokumentenarchivar. "
                "Extrahiere ausschließlich im Dokument belegte Angaben; fehlende oder widersprüchliche Werte bleiben null. "
                "Ersetze ein fehlendes Dokumentdatum niemals durch das heutige Datum. "
                "Antworte nur als JSON mit document_date (YYYY-MM-DD|null), title, document_type, "
                "tags (Liste kontrollierter Stichwörter), issuer, recipient, owner, language, reference_ids, "
                "period, amount, currency, field_confidence und evidence. "
                "Belegzitate müssen wortgetreu aus der Eingabe stammen."
            ),
            "image_description": (
                "Du bist ein präzises Vision-Modell zur Bildbeschreibung. "
                "Beschreibe das übergebene Bild detailliert auf Deutsch. "
                "Erfasse visuelle Elemente, Diagramme, Grafiken, Zeichnungen, "
                "Fotos oder handschriftliche Skizzen sowie eventuell vorhandenen kurzen Text. "
                "Gib NUR die Beschreibung zurück. Keine Einleitung, kein 'Hier ist die Beschreibung'."
            ),
        }
        self.settings = self.load()

    def _defaults(self) -> dict:
        model_defaults = default_recommendation().as_settings_models()
        return {
            "base_dir": "C:\\OCR_Workdir",
            "additional_consume_dirs": [],
            "output_format": "PDF und DOCX",
            "docx_mode": "Lesbare DOCX",
            "models": model_defaults,
            "think_fusion": False,
            "think_analysis": False,
            "organize_enabled": True,
            "confirm_sorting_each_document": False,
            "gdrive_enabled": False,
            "privacy_mode": "standard",
            "redact_cloud_inputs": False,
            "gdrive_credentials_path": normalize_credentials_path(None),
            "gdrive_token_path": normalize_token_path(None),
            "save_docx_enabled": True,
            "save_json_enabled": True,
            "gdrive_upload_pdf": True,
            "gdrive_upload_docx": False,
            "gdrive_upload_json": False,
            "synology_enabled": False,
            "synology_base_url": "",
            "synology_username": "",
            "synology_password": "",
            "synology_password_storage": "credential_manager",
            "synology_root_path": "",
            "synology_upload_pdf": True,
            "synology_upload_docx": False,
            "synology_upload_json": False,
            "unload_models_enabled": True,
            "system_tray_enabled": True,
            "review_before_save": False,
            "large_pdf_reduced": False,
            "large_pdf_page_limit": 20,
            "ocr_languages": "deu+eng",
            "ocr_mode": "auto",
            "onboarding_completed": False,
            "force_pipeline": False,
            "debug_artifacts_enabled": True,
            "prompt_version": self.CURRENT_PROMPT_VERSION,
            "prompts": self.default_prompts.copy(),
        }

    def _apply_defaults(self, data: dict) -> dict:
        defaults = self._defaults()
        source = data or {}
        try:
            source_prompt_version = int(source.get("prompt_version") or 1)
        except (TypeError, ValueError):
            source_prompt_version = 1
        normalized = {**defaults, **source}

        normalized["models"] = dict(normalized.get("models") or {})
        for key, value in defaults["models"].items():
            normalized["models"].setdefault(key, value)

        normalized["prompts"] = dict(normalized.get("prompts") or {})
        for key, value in self.default_prompts.items():
            if key not in normalized["prompts"] or not normalized["prompts"][key]:
                normalized["prompts"][key] = value

        if source_prompt_version < self.CURRENT_PROMPT_VERSION:
            normalized["prompts"] = self._migrate_unsafe_legacy_prompts(normalized["prompts"])
            normalized["prompt_version"] = self.CURRENT_PROMPT_VERSION

        normalized["prompt_version"] = int(normalized.get("prompt_version") or self.CURRENT_PROMPT_VERSION)
        normalized["gdrive_token_path"] = normalize_token_path(normalized.get("gdrive_token_path"))
        normalized["gdrive_credentials_path"] = normalize_credentials_path(normalized.get("gdrive_credentials_path"))
        normalized["additional_consume_dirs"] = self._normalize_path_list(normalized.get("additional_consume_dirs"))
        normalized["large_pdf_page_limit"] = self._normalize_int(
            normalized.get("large_pdf_page_limit"),
            default=20,
            minimum=1,
            maximum=1000,
        )
        try:
            normalized["ocr_languages"] = "+".join(
                normalize_ocr_languages(normalized.get("ocr_languages") or "deu+eng")
            )
        except ValueError:
            normalized["ocr_languages"] = "deu+eng"
        try:
            normalized["ocr_mode"] = normalize_ocr_mode(normalized.get("ocr_mode"))
        except ValueError:
            normalized["ocr_mode"] = "auto"
        normalized["synology_password"] = self._resolve_synology_password(normalized.get("synology_password"))
        return normalized

    def _migrate_unsafe_legacy_prompts(self, prompts: dict) -> dict:
        """Replace only recognisable historic defaults, preserving real custom prompts."""
        migrated = dict(prompts or {})
        unsafe_markers = {
            "vision": (
                "medizinischer ocr-korrektor",
                "korrigiere fehler, ergänze fehlendes",
            ),
            "fusion": (
                "medizinischen dokumentenverarbeitung",
                "fehlerfreien, flüssigen fließtext",
                "standard-ausgabe ist deutsch",
            ),
            "analysis": (
                "medizinischer archivar",
                "sonst das heutige",
                "sonst heute",
                "tags: 3-5",
            ),
        }
        for task, markers in unsafe_markers.items():
            current = str(migrated.get(task) or "")
            folded = current.casefold()
            if any(marker in folded for marker in markers):
                migrated[task] = self.default_prompts[task]
        return migrated

    def _resolve_synology_password(self, value) -> str:
        if not value:
            return ""
        if is_secret_ref(value):
            return load_secret(value)
        return str(value)

    def _normalize_int(self, value, *, default: int, minimum: int, maximum: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = default
        return max(minimum, min(parsed, maximum))

    def _normalize_path_list(self, value) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            candidates = [line.strip() for line in value.splitlines()]
        elif isinstance(value, (list, tuple, set)):
            candidates = [str(item).strip() for item in value]
        else:
            return []

        result = []
        seen = set()
        for item in candidates:
            if not item:
                continue
            path = str(Path(item).expanduser())
            key = path.lower()
            if key in seen:
                continue
            result.append(path)
            seen.add(key)
        return result

    def _is_same_or_child_path(self, child: Path, parent: Path) -> bool:
        try:
            child_key = os.path.normcase(str(child.resolve(strict=False)))
            parent_key = os.path.normcase(str(parent.resolve(strict=False)))
            return os.path.commonpath([child_key, parent_key]) == parent_key
        except (OSError, ValueError):
            return False

    def _load_json_file(self, path: Path) -> dict | None:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as exc:
            logger.warning("Konnte Einstellungen nicht lesen (%s): %s", path, exc)
            return None

    def backup_path(self) -> Path:
        if self.settings_file.suffix:
            return self.settings_file.with_suffix(self.settings_file.suffix + ".bak")
        return self.settings_file.with_name(self.settings_file.name + ".bak")

    def _remove_temp_file(self, temp_path: Path) -> None:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError as exc:
            logger.debug("Konnte temporaere Einstellungsdatei nicht entfernen (%s): %s", temp_path, exc)

    def _write_settings_atomically(self, settings: dict) -> None:
        serialized = json.dumps(settings, indent=4, ensure_ascii=False) + "\n"
        self.settings_file.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.settings_file.with_name(f"{self.settings_file.name}.tmp")

        try:
            temp_path.write_text(serialized, encoding="utf-8")
            harden_private_file(temp_path)

            if self.settings_file.exists():
                backup = self.backup_path()
                self._write_sanitized_backup(backup)
                harden_private_file(backup)

            os.replace(temp_path, self.settings_file)
            harden_private_file(self.settings_file)
        except Exception:
            self._remove_temp_file(temp_path)
            raise

    def _write_sanitized_backup(self, backup: Path) -> None:
        current = self._load_json_file(self.settings_file)
        if current is not None:
            backup.write_text(
                json.dumps(self._settings_for_disk(current), indent=4, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            return
        shutil.copy2(self.settings_file, backup)

    def _settings_for_disk(self, settings: dict) -> dict:
        disk_settings = dict(settings or {})
        password = str(disk_settings.get("synology_password") or "")
        if password:
            if is_secret_ref(password):
                disk_settings["synology_password_storage"] = "credential_manager"
            else:
                stored_ref = store_secret(SYNOLOGY_PASSWORD_NAME, password)
                if stored_ref:
                    disk_settings["synology_password"] = stored_ref
                    disk_settings["synology_password_storage"] = "credential_manager"
                else:
                    disk_settings["synology_password_storage"] = "settings_plaintext"
        else:
            delete_secret(SYNOLOGY_PASSWORD_NAME)
            disk_settings["synology_password"] = ""
            disk_settings["synology_password_storage"] = "none"
        return disk_settings

    def load(self) -> dict:
        if self.settings_file.exists():
            data = self._load_json_file(self.settings_file)
            if data is not None:
                return self._apply_defaults(data)

        if self.uses_default_location and legacy_settings_path().exists():
            data = self._load_json_file(legacy_settings_path())
            if data is not None:
                settings = self._apply_defaults(data)
                try:
                    self.save(settings)
                    logger.info("Legacy-Einstellungen nach %s migriert.", self.settings_file)
                    try:
                        legacy_settings_path().unlink()
                    except OSError:
                        pass
                except Exception as exc:
                    logger.warning("Legacy-Einstellungen konnten nicht migriert werden: %s", exc)
                return settings

        return self._defaults()

    def save(self, settings: dict):
        settings = self._apply_defaults(settings)
        self.validate(settings)
        try:
            self._write_settings_atomically(self._settings_for_disk(settings))
            self.settings = settings
        except Exception as e:
            raise RuntimeError(f"Konnte Einstellungen nicht speichern: {e}")

    def validate(self, settings: dict):
        base_dir = settings.get("base_dir", "").strip()
        if not base_dir:
            raise ValueError("Basis-Verzeichnis darf nicht leer sein.")

        try:
            p = Path(base_dir)
            if not p.is_absolute():
                raise ValueError("Das Basis-Verzeichnis muss ein absoluter Pfad sein.")
        except Exception as e:
            raise ValueError(f"Ungültiger Pfad für Basis-Verzeichnis: {e}")

        base_path = Path(base_dir).resolve(strict=False)
        additional_dirs = self._normalize_path_list(settings.get("additional_consume_dirs"))
        primary_consume = (base_path / "consume").resolve(strict=False)
        reserved_dirs = {
            "original": (base_path / "original").resolve(strict=False),
            "final": (base_path / "final").resolve(strict=False),
            "error": (base_path / "error").resolve(strict=False),
            "work": (base_path / "work").resolve(strict=False),
            "logs": (base_path / "logs").resolve(strict=False),
        }
        cleaned_additional_dirs = []
        for directory in additional_dirs:
            path = Path(directory)
            if not path.is_absolute():
                raise ValueError(f"Zusaetzlicher Eingangsordner muss absolut sein: {directory}")
            resolved = path.resolve(strict=False)
            if resolved == primary_consume:
                continue
            if resolved == base_path:
                raise ValueError(f"'{directory}' ist das Basis-Verzeichnis und darf kein Eingang sein.")
            for label, reserved in reserved_dirs.items():
                if self._is_same_or_child_path(resolved, reserved):
                    raise ValueError(
                        f"'{directory}' liegt im reservierten App-Bereich ({label}) und darf kein Eingang sein."
                    )
            cleaned_additional_dirs.append(str(path))
        settings["additional_consume_dirs"] = cleaned_additional_dirs

        fmt = settings.get("output_format")
        valid_formats = ["Nur PDF", "Nur TXT", "PDF und TXT", "Nur DOCX", "PDF und DOCX"]
        if fmt not in valid_formats:
            raise ValueError(f"Ungültiges Ausgabeformat: {fmt}. Erlaubt sind: {valid_formats}")

        docx_mode = settings.get("docx_mode")
        valid_docx_modes = ["Lesbare DOCX", "Prüf-DOCX", "Originalgetreue DOCX"]
        if docx_mode not in valid_docx_modes:
            raise ValueError(f"Ungültiger DOCX-Modus: {docx_mode}. Erlaubt sind: {valid_docx_modes}")

        privacy_mode = settings.get("privacy_mode", "standard")
        valid_privacy_modes = ["standard", "local_only"]
        if privacy_mode not in valid_privacy_modes:
            raise ValueError(f"Ungültiger Datenschutzmodus: {privacy_mode}. Erlaubt sind: {valid_privacy_modes}")

        try:
            page_limit = int(settings.get("large_pdf_page_limit", 20))
        except (TypeError, ValueError) as exc:
            raise ValueError("large_pdf_page_limit muss eine ganze Zahl sein.") from exc
        if not 1 <= page_limit <= 1000:
            raise ValueError("large_pdf_page_limit muss zwischen 1 und 1000 liegen.")
        settings["large_pdf_page_limit"] = page_limit

        try:
            settings["ocr_languages"] = "+".join(
                normalize_ocr_languages(settings.get("ocr_languages"))
            )
        except ValueError as exc:
            raise ValueError(f"Ungültige OCR-Sprachen: {exc}") from exc
        try:
            settings["ocr_mode"] = normalize_ocr_mode(settings.get("ocr_mode"))
        except ValueError as exc:
            raise ValueError(str(exc)) from exc

        models = settings.get("models", {})
        for key in ["vision", "fusion", "analysis", "glm_ocr"]:
            if key not in models:
                raise ValueError(f"Modell-Einstellung für '{key}' fehlt.")
            if not isinstance(models[key], str):
                raise ValueError(f"Modellname für '{key}' muss ein String sein.")

        bool_fields = [
            ("organize_enabled", True),
            ("confirm_sorting_each_document", False),
            ("gdrive_enabled", False),
            ("save_docx_enabled", True),
            ("save_json_enabled", True),
            ("gdrive_upload_pdf", True),
            ("gdrive_upload_docx", False),
            ("gdrive_upload_json", False),
            ("synology_enabled", False),
            ("synology_upload_pdf", True),
            ("synology_upload_docx", False),
            ("synology_upload_json", False),
            ("unload_models_enabled", True),
            ("system_tray_enabled", True),
            ("review_before_save", False),
            ("redact_cloud_inputs", False),
            ("large_pdf_reduced", False),
            ("onboarding_completed", False),
            ("think_fusion", False),
            ("think_analysis", False),
            ("force_pipeline", False),
            ("debug_artifacts_enabled", True),
        ]
        for key, default in bool_fields:
            value = settings.get(key)
            if value is None:
                settings[key] = default
            elif not isinstance(value, bool):
                raise ValueError(f"{key} muss ein Boolean sein.")

        for key in ["gdrive_credentials_path", "gdrive_token_path"]:
            value = settings.get(key)
            if not value:
                settings[key] = normalize_credentials_path(None) if key == "gdrive_credentials_path" else normalize_token_path(None)
            elif not isinstance(value, str):
                raise ValueError(f"{key} muss ein String sein.")

        for key in ["synology_base_url", "synology_username", "synology_password", "synology_root_path"]:
            value = settings.get(key, "")
            if value is None:
                settings[key] = ""
            elif not isinstance(value, str):
                raise ValueError(f"{key} muss ein String sein.")

        storage = settings.get("synology_password_storage", "credential_manager")
        if storage not in {"credential_manager", "settings_plaintext", "none"}:
            raise ValueError("synology_password_storage hat einen ungueltigen Wert.")

        prompts = settings.get("prompts", {})
        if not isinstance(prompts, dict):
            raise ValueError("prompts muss ein Dictionary sein.")

        for key in ["vision", "fusion", "analysis", "image_description"]:
            if key not in prompts or not isinstance(prompts[key], str):
                raise ValueError(f"Prompt für '{key}' fehlt oder ist kein String.")
