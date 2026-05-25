import shutil
import logging
from pathlib import Path

logger = logging.getLogger("UnifiedOCR")

class DocumentOrganizer:
    """
    Verschiebt exportierte Dateien eines verarbeiteten Dokuments
    in die vom LLM (oder Benutzer) bestimmte Ordnerstruktur unterhalb von final/.
    """
    def __init__(self, final_dir: Path):
        self.final_dir = Path(final_dir)

    def organize(self, final_name: str, target_path: str) -> list:
        """
        Sucht alle exportierten Dateien, die mit `final_name` beginnen,
        und verschiebt sie in `final_dir / target_path`.
        
        Gibt eine Liste der neuen Pfade der verschobenen Dateien zurück.
        """
        # Zielverzeichnis aufbauen
        target_dir = self.final_dir / target_path.strip().replace("\\", "/")
        
        # Sicherstellen, dass das Zielverzeichnis existiert
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logger.error(f"Konnte Zielverzeichnis '{target_dir}' nicht erstellen: {e}")
            # Fallback: Nutzen das direkte final_dir
            target_dir = self.final_dir
            
        moved_files = []
        
        # Alle Dateien in final_dir scannen, die mit final_name beginnen und PDF/TXT sind
        if self.final_dir.exists():
            for item in self.final_dir.iterdir():
                if item.is_file() and item.name.startswith(final_name) and item.suffix.lower() in (".pdf", ".txt", ".docx", ".odt", ".doc", ".odoc"):
                    dest_path = target_dir / item.name
                    
                    # Falls am Zielort bereits eine Datei existiert, überschreiben wir sie
                    try:
                        logger.info(f"Verschiebe {item.name} nach {target_path}...")
                        shutil.move(str(item), str(dest_path))
                        moved_files.append(dest_path)
                    except Exception as e:
                        logger.error(f"Fehler beim Verschieben von {item.name} nach {dest_path}: {e}")
                        
        return moved_files
