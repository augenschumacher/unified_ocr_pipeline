import argparse
import sys
import shutil
import logging
from pathlib import Path

# Projektpfade registrieren, damit relative Importe funktionieren
root = Path(__file__).resolve().parent
sys.path.insert(0, str(root))
sys.path.insert(0, str(root / "unified_ocr_app"))

from core.config import AppConfig, setup_paths
from core.settings import SettingsManager
from core.llm import LLMClient
from core.pipeline import PipelineOrchestrator

# Logger für CLI-Ausgabe auf der Standardausgabe einrichten
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("UnifiedOCR")


def run_cli(file_path: str = None, dir_path: str = None, force: bool = False, base_dir_override: str = None):
    """
    Führt die OCR- und LLM-Pipeline headless über das Terminal aus.
    """
    setup_paths()
    
    # Einstellungen laden
    settings_manager = SettingsManager()
    settings = settings_manager.load()
    
    base_dir = base_dir_override or settings.get("base_dir", "C:\\OCR_Workdir")
    logger.info(f"Verwende Basis-Verzeichnis: {base_dir}")
    
    config = AppConfig(base_dir)
    config.ensure_directories()
    
    # LLM-Modelle aus settings.json laden
    models = settings.get("models", {})
    prompts = settings.get("prompts", {})
    unload_models = settings.get("unload_models_enabled", True)
    keep_alive = "0" if unload_models else "15m"
    
    # Client instanziieren (mit force_pipeline Übergabe für Cache-Bypass)
    llm_client = LLMClient(
        vision_model=models.get("vision"),
        fusion_model=models.get("fusion"),
        analysis_model=models.get("analysis"),
        glm_ocr_model=models.get("glm_ocr"),
        prompts=prompts,
        log_callback=lambda m: logger.info(f"[LLM] {m}"),
        think_fusion=settings.get("think_fusion", False),
        think_analysis=settings.get("think_analysis", False),
        keep_alive=keep_alive,
        prompt_version=settings.get("prompt_version", 1),
        force_pipeline=force
    )
    
    # Orchestrator aufbauen
    orchestrator = PipelineOrchestrator(
        config=config,
        llm_client=llm_client,
        output_format=settings.get("output_format", "PDF und DOCX"),
        docx_mode=settings.get("docx_mode", "Lesbare DOCX"),
        log_callback=lambda m: logger.info(f"[Pipeline] {m}"),
        progress_callback=lambda p: logger.info(f"Fortschritt: {p*100:.1f}%"),
        organize_enabled=settings.get("organize_enabled", True),
        gdrive_enabled=settings.get("gdrive_enabled", False),
        gdrive_token_path=settings.get("gdrive_token_path"),
        save_docx_enabled=settings.get("save_docx_enabled", True),
        save_json_enabled=settings.get("save_json_enabled", True),
        gdrive_upload_pdf=settings.get("gdrive_upload_pdf", True),
        gdrive_upload_docx=settings.get("gdrive_upload_docx", False),
        gdrive_upload_json=settings.get("gdrive_upload_json", False),
        review_before_save=False,  # Im CLI-Modus immer unüberwacht verarbeiten
        large_pdf_reduced=settings.get("large_pdf_reduced", True)
    )
    
    # Verarbeitung starten
    if file_path:
        p = Path(file_path)
        if not p.exists():
            logger.error(f"Eingabedatei '{file_path}' existiert nicht.")
            sys.exit(1)
        logger.info(f"Starte Verarbeitung für Einzeldatei: {p.name}")
        orchestrator.process_file(p)
    elif dir_path:
        d = Path(dir_path)
        if not d.exists() or not d.is_dir():
            logger.error(f"Eingabeverzeichnis '{dir_path}' existiert nicht.")
            sys.exit(1)
        
        # Erlaubte Dateitypen filtern
        allowed_exts = [".pdf", ".png", ".jpg", ".jpeg", ".heic", ".docx", ".odt", ".doc", ".odoc"]
        candidates = [f for f in d.iterdir() if f.is_file() and f.suffix.lower() in allowed_exts]
        
        if not candidates:
            logger.info("Keine verarbeitbaren Dokumente im angegebenen Ordner gefunden.")
            return
            
        logger.info(f"Gefunden: {len(candidates)} Dokumente zur Verarbeitung.")
        for idx, f in enumerate(candidates, 1):
            logger.info(f"\n[{idx}/{len(candidates)}] Verarbeite Datei: {f.name}")
            try:
                orchestrator.process_file(f)
            except Exception as e:
                logger.error(f"Fehler bei der Verarbeitung von {f.name}: {e}")


def main():
    parser = argparse.ArgumentParser(
        description="Unified OCR & LLM Pipeline - CLI & GUI Interface",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--file", "-f",
        type=str,
        help="Pfad zu einer PDF-, Bild- oder Office-Datei zur unmittelbaren Verarbeitung."
    )
    parser.add_argument(
        "--dir", "-d",
        type=str,
        help="Pfad zu einem Ordner mit Dokumenten, die sequenziell verarbeitet werden sollen."
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Erzwingt die komplette Verarbeitung durch die LLMs und überspringt/überschreibt den SQLite-Cache."
    )
    parser.add_argument(
        "--base-dir",
        type=str,
        help="Überschreibt das in den Einstellungen konfigurierte Basis-Arbeitsverzeichnis."
    )

    args = parser.parse_args()

    # Falls Datei oder Ordner übergeben wurden, führe die CLI-Pipeline aus
    if args.file or args.dir:
        try:
            run_cli(file_path=args.file, dir_path=args.dir, force=args.force, base_dir_override=args.base_dir)
        except KeyboardInterrupt:
            logger.info("Verarbeitung durch Benutzer abgebrochen.")
            sys.exit(0)
    else:
        # Andernfalls GUI starten
        try:
            logger.info("Keine CLI-Argumente übergeben. Starte grafische Benutzeroberfläche...")
            from unified_ocr_app.app import main as run_gui
            run_gui()
        except Exception as e:
            logger.error(f"Konnte GUI nicht initialisieren: {e}")
            logger.info("Bitte starten Sie mit --help für CLI-Optionen.")
            sys.exit(1)


if __name__ == "__main__":
    main()
