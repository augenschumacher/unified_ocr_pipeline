import json
import logging
from pathlib import Path
import os

logger = logging.getLogger("UnifiedOCR")

DEFAULT_REGISTRY = {
    "persons": [],
    "known_paths": [],
    "drive_folders": {}
}

class FolderRegistry:
    """
    Verwaltet die Liste registrierter Personen und Ordnerpfade in folder_registry.json.
    Verhindert unkontrollierten Wildwuchs an Unterordnern.
    """
    def __init__(self, base_dir: Path):
        self.base_dir = Path(base_dir)
        self.registry_file = self.base_dir / "folder_registry.json"
        self.data = self._load()

    def _load(self) -> dict:
        if self.registry_file.exists():
            try:
                with open(self.registry_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                
                # Fehlende Schlüssel mit Defaults befüllen
                data.setdefault("persons", DEFAULT_REGISTRY["persons"])
                data.setdefault("known_paths", DEFAULT_REGISTRY["known_paths"])
                data.setdefault("drive_folders", DEFAULT_REGISTRY["drive_folders"])
                return data
            except Exception as e:
                logger.error(f"Fehler beim Laden der folder_registry.json: {e}. Nutze Standardwerte.")
        
        # Falls nicht vorhanden oder Fehler beim Lesen, erstelle Datei mit Default-Werten
        import copy
        data = copy.deepcopy(DEFAULT_REGISTRY)
        self._save_data(data)
        return data

    def _save_data(self, data: dict):
        try:
            self.base_dir.mkdir(parents=True, exist_ok=True)
            tmp_file = self.registry_file.with_suffix(".json.tmp")
            backup_file = self.registry_file.with_suffix(".backup.json")
            if self.registry_file.exists():
                try:
                    backup_file.write_text(self.registry_file.read_text(encoding="utf-8"), encoding="utf-8")
                except OSError:
                    logger.warning("Konnte Backup der folder_registry.json nicht schreiben.")
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=4, ensure_ascii=False)
            os.replace(tmp_file, self.registry_file)
        except Exception as e:
            logger.error(f"Fehler beim Schreiben der folder_registry.json: {e}")

    def save(self):
        """Speichert den aktuellen Zustand der Registry."""
        self._save_data(self.data)

    def get_known_paths(self) -> list:
        """Gibt die Liste aller bekannten Pfade zurück."""
        return self.data.get("known_paths", [])

    def get_persons(self) -> list:
        """Gibt die Liste aller Personen zurück."""
        return self.data.get("persons", [])

    def get_drive_folder_map(self) -> dict:
        """Gibt die gespeicherten Google-Drive-Ordner-IDs pro Pfad zurück."""
        mapping = self.data.get("drive_folders", {})
        return mapping if isinstance(mapping, dict) else {}

    def get_drive_folder_id(self, path: str) -> str | None:
        normalized = path.strip().replace("\\", "/")
        return self.get_drive_folder_map().get(normalized)

    def set_drive_folder_id(self, path: str, folder_id: str):
        normalized = path.strip().replace("\\", "/")
        if not normalized or not folder_id:
            return
        mapping = dict(self.get_drive_folder_map())
        mapping[normalized] = folder_id
        self.data["drive_folders"] = mapping

    def prune_drive_folder_map(self):
        known = set(self.get_known_paths())
        self.data["drive_folders"] = {
            k: v for k, v in self.get_drive_folder_map().items() if k in known
        }

    def add_path(self, path: str) -> bool:
        """
        Fügt einen Pfad hinzu, falls er noch nicht existiert.
        Erwartet Pfad im Format 'Person/Kategorie' oder 'Sonstiges'.
        Der Hauptordner (erste Stufe) muss einer der erlaubten Hauptordner sein.
        """
        path = path.strip().replace("\\", "/")
        if not path:
            return False
            
        parts = [p.strip() for p in path.split("/") if p.strip()]
        if not parts:
            return False
            
        valid_persons = self.get_persons()
        person_matched = next((vp for vp in valid_persons if vp.lower() == parts[0].lower()), None)
        if not person_matched:
            logger.warning(f"Pfad '{path}' abgelehnt: Hauptordner '{parts[0]}' ist nicht in {valid_persons} enthalten.")
            return False
            
        # Normalisiere den Hauptordner
        parts[0] = person_matched
        normalized_path = "/".join(parts)
        
        known = self.get_known_paths()
        if normalized_path in known:
            return False
            
        known.append(normalized_path)
        self.data["known_paths"] = sorted(known)
        self.save()
        return True

    def add_person(self, person: str) -> bool:
        """Fügt eine neue Person hinzu, falls noch nicht vorhanden (nur intern/Legacy-Kompatibilität)."""
        person = person.strip()
        if not person:
            return False
            
        persons = self.get_persons()
        if person in persons:
            return False
            
        persons.append(person)
        self.data["persons"] = persons
        self.save()
        return True

    def get_tree(self) -> dict:
        """
        Rekonstruiert eine verschachtelte Dictionary-Struktur aus Personen und Pfaden.
        """
        tree = {}
        for person in self.get_persons():
            tree[person] = {}
            
        for path in self.get_known_paths():
            parts = [p.strip() for p in path.split("/") if p.strip()]
            if not parts:
                continue
            # Sicherstellen, dass die Person im Baum existiert
            primary = parts[0]
            if primary not in tree:
                tree[primary] = {}
                
            current = tree[primary]
            for part in parts[1:]:
                if part not in current:
                    current[part] = {}
                current = current[part]
        return tree

    def save_tree(self, tree: dict):
        """
        Konvertiert eine Baumstruktur zurück in Personen und bekannte Pfade und speichert diese.
        """
        persons = list(tree.keys())
        known_paths = []
        
        def traverse(node, prefix):
            for k, v in node.items():
                current_path = f"{prefix}/{k}" if prefix else k
                known_paths.append(current_path)
                traverse(v, current_path)
                
        for person, subtree in tree.items():
            known_paths.append(person)
            traverse(subtree, person)
            
        known_paths = sorted(list(set(known_paths)))
        old_drive_map = self.get_drive_folder_map()
        self.data["drive_folders"] = {k: v for k, v in old_drive_map.items() if k in known_paths}
        self.data["persons"] = persons
        self.data["known_paths"] = known_paths
        self.save()
