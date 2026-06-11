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


logger = logging.getLogger("UnifiedOCR")


class SettingsManager:
    CURRENT_PROMPT_VERSION = 2

    def __init__(self, settings_file: str | Path | None = None):
        self.uses_default_location = settings_file is None
        self.settings_file = Path(settings_file) if settings_file is not None else default_settings_path()
        self.default_prompts = {
            "vision": (
                "Du bist ein medizinischer OCR-Korrektor und Layout-Analyst. "
                "Dir wird ein Bild einer Dokumentenseite und die vorläufig extrahierten Markdown-Chunks dieser Seite übergeben. "
                "Prüfe das OCR-Ergebnis (Tabellenstrukturen, Absätze, visuelle Formatierungen wie Überschriften, Fettung, Kursivschrift) kritisch anhand des Bildes. "
                "Korrigiere Fehler, ergänze Fehlendes und gib das finale, bereinigte Zwischen-Markdown für diese Seite zurück. "
                "WICHTIG: Wenn du eine Tabelle erkennst, formatiere sie als saubere Markdown-Tabelle und umschließe sie ZWINGEND mit den Tags <table_block> und </table_block>. "
                "Gib NUR das bereinigte Markdown zurück. Keine Einleitung, keine Kommentare."
            ),
            "fusion": (
                "Du bist ein KI-Assistent zur medizinischen Dokumentenverarbeitung. "
                "Erstelle aus den bereitgestellten Texten für diese EINE Seite einen fehlerfreien, flüssigen Fließtext. "
                "Das 'Bereinigte Zwischen-Markdown' enthält die geprüfte Layout-Struktur und den Text. Der 'OCR Rohtext' dient als Absicherung für eventuelle Auslassungen. "
                "Achte extrem penibel auf deutsche Umlaute (ä, ö, ü) und das Eszett (ß). "
                "SPRACH-LOGIK: Die Standard-Ausgabe ist DEUTSCH. Achte penibel auf korrekte deutsche Umlautschreibweise. "
                "AUSNAHME: Wenn du im bereitgestellten Quelltext eindeutig eine andere Sprache (z. B. Englisch) erkennst, passe die Zielsprache automatisch an diese an. "
                "ÜBERGANGS-KONTEXT: Falls der Kontext der vorherigen Seite bereitgestellt wurde, nutze ihn AUSSCHLIESSLICH, um abgebrochene Sätze am Seitenübergang logisch, grammatikalisch korrekt und nahtlos fortzuführen. Gib den Text der vorherigen Seite unter keinen Umständen erneut aus! "
                "Formatierung: Behalte Überschriften, Listen, Tabellen und Textauszeichnungen (Fett, Kursiv) so getreu wie möglich bei. "
                "TABELLEN-SCHUTZ: Falls im Quelltext Blöcke mit <table_block> und </table_block> umschlossen sind, musst du diese Blöcke und deren gesamten Inhalt ABSOLUT UNVERÄNDERT übernehmen! Ändere kein einziges Zeichen, keine Pipes und keine Ausrichtungen innerhalb dieser Tags. "
                "Gib NUR den finalen korrigierten Text für diese Seite zurück. Schreibe absolut keine Einleitung, Erklärung oder Metakommentare."
            ),
            "analysis": (
                "Du bist ein medizinischer Archivar. Extrahiere aus dem Text:\n"
                "1. date: Ein Datum im Format dd-mm-yyyy (aus dem Dokument, sonst das heutige)\n"
                "2. title: Einen passenden kurzen Titel (keine Leerzeichen, nutze Unterstriche)\n"
                "3. document_type: Den Dokumententyp (z.B. Arztbrief, Rechnung, Befund)\n"
                "4. tags: 3-5 relevante Stichworte (kommagetrennt)\n"
                "Antworte EXAKT im JSON Format: {\"date\": \"...\", \"title\": \"...\", \"document_type\": \"...\", \"tags\": \"...\"}"
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
        return {
            "base_dir": "C:\\OCR_Workdir",
            "additional_consume_dirs": [],
            "output_format": "PDF und DOCX",
            "docx_mode": "Lesbare DOCX",
            "models": {
                "vision": "qwen3-vl:30b-a3b-instruct-q4_K_M",
                "fusion": "qwen3.6:27b",
                "analysis": "qwen3.6:27b",
                "glm_ocr": "glm-ocr:bf16",
            },
            "think_fusion": False,
            "think_analysis": False,
            "organize_enabled": True,
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
            "synology_root_path": "",
            "synology_upload_pdf": True,
            "synology_upload_docx": False,
            "synology_upload_json": False,
            "unload_models_enabled": True,
            "system_tray_enabled": True,
            "review_before_save": False,
            "large_pdf_reduced": True,
            "onboarding_completed": False,
            "force_pipeline": False,
            "debug_artifacts_enabled": True,
            "prompt_version": self.CURRENT_PROMPT_VERSION,
            "prompts": self.default_prompts.copy(),
        }

    def _apply_defaults(self, data: dict) -> dict:
        defaults = self._defaults()
        normalized = {**defaults, **(data or {})}

        normalized.setdefault("models", {})
        for key, value in defaults["models"].items():
            normalized["models"].setdefault(key, value)

        normalized.setdefault("prompts", {})
        for key, value in self.default_prompts.items():
            if key not in normalized["prompts"] or not normalized["prompts"][key]:
                normalized["prompts"][key] = value

        normalized["prompt_version"] = int(normalized.get("prompt_version") or self.CURRENT_PROMPT_VERSION)
        normalized["gdrive_token_path"] = normalize_token_path(normalized.get("gdrive_token_path"))
        normalized["gdrive_credentials_path"] = normalize_credentials_path(normalized.get("gdrive_credentials_path"))
        normalized["additional_consume_dirs"] = self._normalize_path_list(normalized.get("additional_consume_dirs"))
        return normalized

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
                shutil.copy2(self.settings_file, backup)
                harden_private_file(backup)

            os.replace(temp_path, self.settings_file)
            harden_private_file(self.settings_file)
        except Exception:
            self._remove_temp_file(temp_path)
            raise

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
            self._write_settings_atomically(settings)
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

        models = settings.get("models", {})
        for key in ["vision", "fusion", "analysis", "glm_ocr"]:
            if key not in models:
                raise ValueError(f"Modell-Einstellung für '{key}' fehlt.")
            if not isinstance(models[key], str):
                raise ValueError(f"Modellname für '{key}' muss ein String sein.")

        bool_fields = [
            ("organize_enabled", True),
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
            ("large_pdf_reduced", True),
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

        prompts = settings.get("prompts", {})
        if not isinstance(prompts, dict):
            raise ValueError("prompts muss ein Dictionary sein.")

        for key in ["vision", "fusion", "analysis", "image_description"]:
            if key not in prompts or not isinstance(prompts[key], str):
                raise ValueError(f"Prompt für '{key}' fehlt oder ist kein String.")
