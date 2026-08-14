"""Persistent folder registry and archive path safety helpers.

All archive paths are stored as relative POSIX-style paths.  Validation lives
in this module so the registry and the file organizer use exactly the same
rules on every platform (including when tests run outside Windows).
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import time
import uuid
from contextlib import contextmanager
from pathlib import Path, PureWindowsPath

logger = logging.getLogger("UnifiedOCR")

DEFAULT_REGISTRY = {
    "revision": 0,
    "persons": [],
    "known_paths": [],
    "drive_folders": {},
    "path_contexts": {},
}

_INVALID_WINDOWS_CHARS = re.compile(r'[<>:"|?*]')
_WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
    "COM¹", "COM²", "COM³", "LPT¹", "LPT²", "LPT³",
}
ARCHIVE_INTERNAL_TOP_LEVEL_NAMES = frozenset({
    "_staging",
    "_transactions",
    "_recovery",
    "_export_reservations",
    "_export_transactions",
    "begleitdateien",
})


class UnsafeArchivePath(ValueError):
    """Raised when an archive-relative path is unsafe or not portable."""


class RegistryWriteError(RuntimeError):
    """Raised when a registry update could not be durably written."""


def normalize_archive_path(
    value: str | os.PathLike[str],
    *,
    default: str | None = None,
    max_depth: int = 16,
    max_length: int = 240,
) -> str:
    """Validate and normalize an archive-relative path.

    The returned value always uses ``/`` separators.  Absolute/drive/UNC
    paths, traversal segments, control characters, Windows-invalid names and
    overlong paths are rejected instead of being partially sanitized.
    """
    raw = str(value or "").strip()
    if not raw:
        if default is not None:
            return normalize_archive_path(default, max_depth=max_depth, max_length=max_length)
        raise UnsafeArchivePath("Der Archivpfad darf nicht leer sein.")

    windows_path = PureWindowsPath(raw)
    if raw.startswith(("/", "\\")) or windows_path.drive or windows_path.is_absolute():
        raise UnsafeArchivePath(f"Absolute Archivpfade sind nicht erlaubt: {raw!r}")

    raw = raw.replace("\\", "/")
    parts: list[str] = []
    for raw_part in raw.split("/"):
        part = raw_part.strip()
        if not part:
            continue
        if part in {".", ".."}:
            raise UnsafeArchivePath(f"Pfadnavigation ist nicht erlaubt: {raw!r}")
        if any(ord(char) < 32 for char in part):
            raise UnsafeArchivePath(f"Steuerzeichen sind im Archivpfad nicht erlaubt: {part!r}")
        if _INVALID_WINDOWS_CHARS.search(part):
            raise UnsafeArchivePath(f"Ungültiges Windows-Zeichen im Archivpfad: {part!r}")
        if part.endswith("."):
            raise UnsafeArchivePath(f"Pfadsegmente dürfen nicht mit einem Punkt enden: {part!r}")
        if len(part) > 255:
            raise UnsafeArchivePath("Ein Archivpfad-Segment ist länger als 255 Zeichen.")

        device_name = part.split(".", 1)[0].rstrip(" .").upper()
        if device_name in _WINDOWS_RESERVED_NAMES:
            raise UnsafeArchivePath(f"Reservierter Windows-Name im Archivpfad: {part!r}")
        parts.append(part)

    if not parts:
        raise UnsafeArchivePath("Der Archivpfad enthält kein gültiges Segment.")
    if len(parts) > max_depth:
        raise UnsafeArchivePath(f"Der Archivpfad darf höchstens {max_depth} Ebenen enthalten.")

    normalized = "/".join(parts)
    if len(normalized) > max_length:
        raise UnsafeArchivePath(f"Der relative Archivpfad darf höchstens {max_length} Zeichen lang sein.")
    return normalized


def ensure_user_archive_namespace(path: str) -> str:
    """Reject top-level names reserved for application-owned archive state."""
    normalized = normalize_archive_path(path)
    top_level = normalized.split("/", 1)[0].casefold()
    if top_level in {name.casefold() for name in ARCHIVE_INTERNAL_TOP_LEVEL_NAMES}:
        raise UnsafeArchivePath(
            f"Der Hauptordner '{normalized.split('/', 1)[0]}' ist für interne Programmdaten reserviert."
        )
    return normalized


# Descriptive alias for callers that prefer the explicit name.
normalize_relative_archive_path = normalize_archive_path


def resolve_archive_target(root: Path, relative_path: str | os.PathLike[str]) -> Path:
    """Resolve a validated path below *root*, following existing symlinks.

    Containment is checked before callers create directories.  This also
    rejects an otherwise harmless-looking relative path if an existing parent
    directory is a symlink/junction leading outside the archive root.
    """
    normalized = normalize_archive_path(relative_path)
    root_display = Path(root).absolute()
    root_resolved = root_display.resolve()
    candidate = root_display.joinpath(*normalized.split("/"))
    candidate_resolved = candidate.resolve(strict=False)
    try:
        candidate_resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise UnsafeArchivePath(
            f"Zielpfad liegt außerhalb des Archivordners: {candidate_resolved}"
        ) from exc
    return candidate


class FolderRegistry:
    """Manage the controlled archive folder tree in ``folder_registry.json``."""

    def __init__(self, base_dir: Path):
        self.base_dir = Path(base_dir)
        self.registry_file = self.base_dir / "folder_registry.json"
        self.data = self._load()

    def _load(self) -> dict:
        if self.registry_file.exists():
            try:
                return self._read_registry_file(self.registry_file)
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                logger.error(
                    "Fehler beim Laden der folder_registry.json: %s. "
                    "Versuche validierte Sicherung.",
                    exc,
                )
                return self._recover_corrupt_primary(exc)

        backup_file = self.registry_file.with_suffix(".backup.json")
        if backup_file.exists():
            try:
                restored = self._read_registry_file(backup_file, strict=True)
                self._write_primary_only(restored)
                logger.warning(
                    "folder_registry.json fehlte und wurde aus der validierten Sicherung wiederhergestellt."
                )
                return restored
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                raise RegistryWriteError(
                    "folder_registry.json fehlt und die vorhandene Sicherung ist ungueltig; "
                    "die Archivzuordnung bleibt aus Sicherheitsgruenden gesperrt."
                ) from exc

        data = copy.deepcopy(DEFAULT_REGISTRY)
        self._save_data(data)
        return data

    def _read_registry_file(self, path: Path, *, strict: bool = False) -> dict:
        with path.open("r", encoding="utf-8") as handle:
            loaded = json.load(handle)
        if not isinstance(loaded, dict):
            raise ValueError("Registry-Wurzel muss ein JSON-Objekt sein.")
        if strict:
            self._validate_registry_shape(loaded)
        return self._sanitize_loaded_data(loaded)

    @staticmethod
    def _validate_registry_shape(loaded: dict) -> None:
        """Reject structurally damaged backups before they become authoritative."""
        try:
            revision = int(loaded.get("revision", 0))
        except (TypeError, ValueError) as exc:
            raise ValueError("Registry-Revision ist ungueltig.") from exc
        if revision < 0:
            raise ValueError("Registry-Revision darf nicht negativ sein.")

        for key in ("persons", "known_paths"):
            if key in loaded and not isinstance(loaded.get(key), list):
                raise ValueError(f"Registry-Feld {key!r} muss eine Liste sein.")
        for key in ("drive_folders", "path_contexts"):
            if key in loaded and not isinstance(loaded.get(key), dict):
                raise ValueError(f"Registry-Feld {key!r} muss ein Objekt sein.")

        for person in loaded.get("persons", []):
            normalized = ensure_user_archive_namespace(str(person))
            if "/" in normalized:
                raise ValueError("Registry-Personen muessen genau eine Ebene sein.")
        for raw_path in loaded.get("known_paths", []):
            ensure_user_archive_namespace(str(raw_path))
        for key in ("drive_folders", "path_contexts"):
            for raw_path, value in loaded.get(key, {}).items():
                ensure_user_archive_namespace(str(raw_path))
                if key == "path_contexts" and not isinstance(value, dict):
                    raise ValueError("Registry-Pfadkontexte muessen Objekte sein.")

    def _recover_corrupt_primary(self, primary_error: Exception) -> dict:
        """Restore a validated backup without ever replacing it by corrupt bytes."""
        backup_file = self.registry_file.with_suffix(".backup.json")
        try:
            restored = self._read_registry_file(backup_file, strict=True)
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as backup_error:
            quarantine = self._quarantine_copy(self.registry_file)
            logger.critical(
                "Registry und Sicherung sind unbrauchbar; fehlerhafte Primaerdatei gesichert unter %s.",
                quarantine,
            )
            raise RegistryWriteError(
                "folder_registry.json ist beschaedigt und es gibt keine gueltige Sicherung; "
                "die Archivzuordnung bleibt aus Sicherheitsgruenden gesperrt."
            ) from backup_error

        quarantine = self._quarantine_move(self.registry_file)
        try:
            self._write_primary_only(restored)
        except Exception as exc:
            # Keep the validated backup untouched and fail closed.  The
            # quarantined original remains available for forensic recovery.
            raise RegistryWriteError(
                "Die validierte Registry-Sicherung konnte nicht wiederhergestellt werden."
            ) from exc
        logger.warning(
            "Beschaedigte folder_registry.json nach %s verschoben und validierte Sicherung wiederhergestellt "
            "(Primaerfehler: %s).",
            quarantine,
            primary_error,
        )
        return restored

    def _quarantine_path(self) -> Path:
        token = f"{int(time.time())}.{uuid.uuid4().hex}"
        return self.registry_file.with_name(f"folder_registry.corrupt.{token}.json")

    def _quarantine_copy(self, source: Path) -> Path:
        quarantine = self._quarantine_path()
        try:
            payload = source.read_bytes()
            with quarantine.open("xb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            raise RegistryWriteError(
                "Beschaedigte folder_registry.json konnte nicht quarantiniert werden."
            ) from exc
        return quarantine

    def _quarantine_move(self, source: Path) -> Path:
        quarantine = self._quarantine_path()
        try:
            os.replace(source, quarantine)
        except OSError as exc:
            raise RegistryWriteError(
                "Beschaedigte folder_registry.json konnte nicht quarantiniert werden."
            ) from exc
        return quarantine

    def _write_primary_only(self, data: dict) -> None:
        """Atomically write the primary registry while preserving its backup."""
        tmp_file: Path | None = None
        try:
            self.base_dir.mkdir(parents=True, exist_ok=True)
            tmp_file = self.registry_file.with_name(
                f".{self.registry_file.name}.{uuid.uuid4().hex}.restore.tmp"
            )
            with tmp_file.open("x", encoding="utf-8", newline="\n") as handle:
                json.dump(data, handle, indent=4, ensure_ascii=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_file, self.registry_file)
            tmp_file = None
        finally:
            if tmp_file is not None:
                try:
                    tmp_file.unlink(missing_ok=True)
                except OSError:
                    logger.warning("Temporäre Restore-Datei konnte nicht entfernt werden: %s", tmp_file)

    def _sanitize_loaded_data(self, loaded: dict) -> dict:
        data = copy.deepcopy(DEFAULT_REGISTRY)
        try:
            data["revision"] = max(0, int(loaded.get("revision", 0)))
        except (TypeError, ValueError):
            data["revision"] = 0

        persons = loaded.get("persons", [])
        if isinstance(persons, list):
            for value in persons:
                try:
                    person = ensure_user_archive_namespace(str(value))
                    if "/" in person:
                        raise UnsafeArchivePath("Personen müssen genau eine Ordnerebene sein.")
                    if person.casefold() not in {p.casefold() for p in data["persons"]}:
                        data["persons"].append(person)
                except UnsafeArchivePath as exc:
                    logger.warning("Unsichere Person aus Registry ignoriert (%r): %s", value, exc)

        known_paths = loaded.get("known_paths", [])
        if isinstance(known_paths, list):
            for value in known_paths:
                try:
                    path = ensure_user_archive_namespace(str(value))
                    if path not in data["known_paths"]:
                        data["known_paths"].append(path)
                except UnsafeArchivePath as exc:
                    logger.warning("Unsicherer Registry-Pfad ignoriert (%r): %s", value, exc)
        data["known_paths"].sort()

        for key in ("drive_folders", "path_contexts"):
            mapping = loaded.get(key, {})
            if not isinstance(mapping, dict):
                continue
            for raw_path, value in mapping.items():
                try:
                    normalized = ensure_user_archive_namespace(str(raw_path))
                except UnsafeArchivePath as exc:
                    logger.warning("Unsicherer Registry-Schlüssel ignoriert (%r): %s", raw_path, exc)
                    continue
                if key == "drive_folders":
                    if value:
                        data[key][normalized] = str(value)
                elif isinstance(value, dict):
                    data[key][normalized] = copy.deepcopy(value)
        return data

    def _save_data(self, data: dict) -> None:
        """Atomically persist *data* and propagate any write failure."""
        tmp_file: Path | None = None
        backup_tmp: Path | None = None
        try:
            self.base_dir.mkdir(parents=True, exist_ok=True)
            token = uuid.uuid4().hex
            tmp_file = self.registry_file.with_name(f".{self.registry_file.name}.{token}.tmp")
            backup_file = self.registry_file.with_suffix(".backup.json")

            if self.registry_file.exists():
                backup_tmp = backup_file.with_name(f".{backup_file.name}.{token}.tmp")
                backup_text = self.registry_file.read_text(encoding="utf-8")
                with backup_tmp.open("x", encoding="utf-8", newline="\n") as handle:
                    handle.write(backup_text)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(backup_tmp, backup_file)
                backup_tmp = None

            with tmp_file.open("x", encoding="utf-8", newline="\n") as handle:
                json.dump(data, handle, indent=4, ensure_ascii=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_file, self.registry_file)
            tmp_file = None
        except Exception as exc:
            logger.error("Fehler beim Schreiben der folder_registry.json: %s", exc)
            raise RegistryWriteError(
                f"folder_registry.json konnte nicht sicher geschrieben werden: {exc}"
            ) from exc
        finally:
            for leftover in (tmp_file, backup_tmp):
                if leftover is not None:
                    try:
                        leftover.unlink(missing_ok=True)
                    except OSError:
                        logger.warning("Temporäre Registry-Datei konnte nicht entfernt werden: %s", leftover)

    def _commit(self, candidate: dict) -> None:
        with self._registry_lock():
            current_revision = 0
            if self.registry_file.exists():
                try:
                    loaded = json.loads(self.registry_file.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        current_revision = max(0, int(loaded.get("revision", 0)))
                except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                    raise RegistryWriteError(
                        f"Aktuelle folder_registry.json konnte für den Versionsvergleich nicht gelesen werden: {exc}"
                    ) from exc
            expected_revision = max(0, int(self.data.get("revision", 0)))
            if current_revision != expected_revision:
                raise RegistryWriteError(
                    "folder_registry.json wurde zwischenzeitlich geändert; bitte neu laden und erneut speichern."
                )
            committed = copy.deepcopy(candidate)
            committed["revision"] = current_revision + 1
            self._save_data(committed)
            self.data = committed

    @contextmanager
    def _registry_lock(self, *, timeout_seconds: float = 10.0):
        """Small cross-process lock guarding registry compare-and-swap commits."""
        lock_path = self.registry_file.with_suffix(".lock")
        deadline = time.monotonic() + max(0.5, float(timeout_seconds))
        descriptor = None
        while descriptor is None:
            try:
                self.base_dir.mkdir(parents=True, exist_ok=True)
                descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.write(descriptor, f"pid={os.getpid()} time={time.time()}\n".encode("ascii"))
            except FileExistsError:
                try:
                    if time.time() - lock_path.stat().st_mtime > 60:
                        lock_path.unlink(missing_ok=True)
                        continue
                except OSError:
                    pass
                if time.monotonic() >= deadline:
                    raise RegistryWriteError(
                        "folder_registry.json ist durch einen anderen Vorgang gesperrt."
                    )
                time.sleep(0.05)
        try:
            yield
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                lock_path.unlink(missing_ok=True)
            except OSError:
                logger.warning("Registry-Sperrdatei konnte nicht entfernt werden: %s", lock_path)

    def save(self) -> None:
        """Persist the current registry, raising :class:`RegistryWriteError` on failure."""
        candidate = self._sanitize_loaded_data(self.data)
        self._commit(candidate)

    def get_known_paths(self) -> list[str]:
        return list(self.data.get("known_paths", []))

    def get_persons(self) -> list[str]:
        return list(self.data.get("persons", []))

    def get_drive_folder_map(self) -> dict:
        mapping = self.data.get("drive_folders", {})
        return dict(mapping) if isinstance(mapping, dict) else {}

    def get_path_contexts(self) -> dict:
        contexts = self.data.get("path_contexts", {})
        return copy.deepcopy(contexts) if isinstance(contexts, dict) else {}

    def get_path_context(self, path: str) -> dict:
        try:
            normalized = normalize_archive_path(path)
        except UnsafeArchivePath:
            return {}
        context = self.get_path_contexts().get(normalized, {})
        return context if isinstance(context, dict) else {}

    def set_path_context(self, path: str, context: dict) -> None:
        normalized = normalize_archive_path(path)
        if not isinstance(context, dict):
            raise TypeError("Der Pfadkontext muss ein Dictionary sein.")
        candidate = copy.deepcopy(self.data)
        contexts = dict(candidate.get("path_contexts", {}))
        cleaned = {
            "object_type": str(context.get("object_type", "")).strip(),
            "aliases": self._clean_context_list(context.get("aliases", [])),
            "keywords": self._clean_context_list(context.get("keywords", [])),
            "notes": str(context.get("notes", "")).strip(),
            "binds_owner": bool(context.get("binds_owner", False)),
        }
        if any(cleaned.values()):
            contexts[normalized] = cleaned
        else:
            contexts.pop(normalized, None)
        candidate["path_contexts"] = contexts
        self._commit(candidate)

    @staticmethod
    def _clean_context_list(values) -> list[str]:
        if isinstance(values, str):
            raw = values.replace("\n", ",").split(",")
        elif isinstance(values, list):
            raw = values
        else:
            raw = []
        cleaned: list[str] = []
        seen: set[str] = set()
        for value in raw:
            item = " ".join(str(value).strip().split())
            key = item.casefold()
            if item and key not in seen:
                cleaned.append(item)
                seen.add(key)
        return cleaned

    def get_drive_folder_id(self, path: str) -> str | None:
        try:
            normalized = normalize_archive_path(path)
        except UnsafeArchivePath:
            return None
        return self.get_drive_folder_map().get(normalized)

    def set_drive_folder_id(self, path: str, folder_id: str) -> None:
        normalized = normalize_archive_path(path)
        if not folder_id:
            return
        mapping = dict(self.get_drive_folder_map())
        mapping[normalized] = str(folder_id)
        self.data["drive_folders"] = mapping

    def prune_drive_folder_map(self) -> None:
        known = set(self.get_known_paths())
        self.data["drive_folders"] = {
            key: value for key, value in self.get_drive_folder_map().items() if key in known
        }

    def prune_path_contexts(self) -> None:
        known = set(self.get_known_paths())
        self.data["path_contexts"] = {
            key: value for key, value in self.get_path_contexts().items() if key in known
        }

    def add_path(self, path: str) -> bool:
        try:
            normalized_input = ensure_user_archive_namespace(path)
        except UnsafeArchivePath as exc:
            logger.warning("Pfad %r abgelehnt: %s", path, exc)
            return False

        parts = normalized_input.split("/")
        valid_persons = self.get_persons()
        person_matched = next(
            (person for person in valid_persons if person.casefold() == parts[0].casefold()),
            None,
        )
        if not person_matched:
            logger.warning(
                "Pfad %r abgelehnt: Hauptordner %r ist nicht in %s enthalten.",
                path,
                parts[0],
                valid_persons,
            )
            return False
        parts[0] = person_matched
        normalized_path = "/".join(parts)
        known = self.get_known_paths()
        if normalized_path in known:
            return False

        candidate = copy.deepcopy(self.data)
        candidate["known_paths"] = sorted([*known, normalized_path])
        self._commit(candidate)
        return True

    def add_person(self, person: str) -> bool:
        try:
            normalized = ensure_user_archive_namespace(person)
        except UnsafeArchivePath as exc:
            logger.warning("Person %r abgelehnt: %s", person, exc)
            return False
        if "/" in normalized:
            logger.warning("Person %r abgelehnt: nur eine Ordnerebene ist erlaubt.", person)
            return False

        persons = self.get_persons()
        if any(existing.casefold() == normalized.casefold() for existing in persons):
            return False
        candidate = copy.deepcopy(self.data)
        candidate["persons"] = [*persons, normalized]
        self._commit(candidate)
        return True

    def get_tree(self) -> dict:
        tree: dict[str, dict] = {person: {} for person in self.get_persons()}
        for path in self.get_known_paths():
            parts = path.split("/")
            current = tree.setdefault(parts[0], {})
            for part in parts[1:]:
                current = current.setdefault(part, {})
        return tree

    def save_tree(self, tree: dict, *, path_contexts: dict | None = None) -> None:
        if not isinstance(tree, dict):
            raise TypeError("Der Ordnerbaum muss ein Dictionary sein.")

        persons: list[str] = []
        known_paths: list[str] = []
        active_nodes: set[int] = set()

        def traverse(node: dict, prefix: str) -> None:
            if not isinstance(node, dict):
                raise TypeError(f"Unterbaum für {prefix!r} muss ein Dictionary sein.")
            node_id = id(node)
            if node_id in active_nodes:
                raise ValueError("Der Ordnerbaum enthält einen Zyklus.")
            active_nodes.add(node_id)
            try:
                for raw_name, child in node.items():
                    name = normalize_archive_path(str(raw_name))
                    if "/" in name:
                        raise UnsafeArchivePath("Baumknoten dürfen nur eine Ordnerebene enthalten.")
                    current_path = normalize_archive_path(f"{prefix}/{name}" if prefix else name)
                    known_paths.append(current_path)
                    traverse(child, current_path)
            finally:
                active_nodes.remove(node_id)

        for raw_person, subtree in tree.items():
            person = ensure_user_archive_namespace(str(raw_person))
            if "/" in person:
                raise UnsafeArchivePath("Personen müssen genau eine Ordnerebene sein.")
            if any(existing.casefold() == person.casefold() for existing in persons):
                raise ValueError(f"Doppelte Person im Ordnerbaum: {person}")
            persons.append(person)
            known_paths.append(person)
            traverse(subtree, person)

        known_paths = sorted(set(known_paths))
        removed_paths = set(self.get_known_paths()) - set(known_paths)
        final_dir = self.base_dir / "final"
        for removed_path in sorted(removed_paths, key=lambda value: value.count("/"), reverse=True):
            target = resolve_archive_target(final_dir, removed_path)
            if not target.exists():
                continue
            contains_records = target.is_symlink() or any(
                child.is_file() or child.is_symlink()
                for child in target.rglob("*")
            )
            if contains_records:
                raise RegistryWriteError(
                    f"Belegter Archivpfad darf nicht aus der Registry entfernt werden: {removed_path}"
                )
        candidate = copy.deepcopy(self.data)
        candidate["persons"] = persons
        candidate["known_paths"] = known_paths
        candidate["drive_folders"] = {
            key: value for key, value in self.get_drive_folder_map().items() if key in known_paths
        }
        context_source = self.get_path_contexts() if path_contexts is None else path_contexts
        cleaned_contexts = {}
        for raw_path, raw_context in (context_source or {}).items():
            if not isinstance(raw_context, dict):
                continue
            try:
                context_path = normalize_archive_path(raw_path)
            except UnsafeArchivePath:
                continue
            if context_path not in known_paths:
                continue
            cleaned = {
                "object_type": str(raw_context.get("object_type", "")).strip(),
                "aliases": self._clean_context_list(raw_context.get("aliases", [])),
                "keywords": self._clean_context_list(raw_context.get("keywords", [])),
                "notes": str(raw_context.get("notes", "")).strip(),
                "binds_owner": bool(raw_context.get("binds_owner", False)),
            }
            if any(cleaned.values()):
                cleaned_contexts[context_path] = cleaned
        candidate["path_contexts"] = cleaned_contexts
        self._commit(candidate)
