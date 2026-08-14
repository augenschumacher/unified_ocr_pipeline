import argparse
import sys
import logging
from pathlib import Path

# Projektpfade registrieren, damit relative Importe funktionieren
root = Path(__file__).resolve().parent
sys.path.insert(0, str(root))
sys.path.insert(0, str(root / "unified_ocr_app"))

from core.config import AppConfig, setup_paths
from core.input_files import collect_cli_inputs, stage_input_file, supported_suffixes_text
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


def run_cli(
    file_path: str = None,
    dir_path: str = None,
    force: bool = False,
    base_dir_override: str = None,
    move_source: bool = False,
):
    """
    Führt die OCR- und LLM-Pipeline headless über das Terminal aus.
    """
    setup_paths()

    # Einstellungen laden
    settings_manager = SettingsManager()
    settings = settings_manager.load()

    base_dir = base_dir_override or settings.get("base_dir", "C:\\OCR_Workdir")
    logger.info(f"Verwende Basis-Verzeichnis: {base_dir}")
    
    config = AppConfig(
        base_dir,
        additional_consume_dirs=settings.get("additional_consume_dirs", []),
        large_pdf_page_limit=settings.get("large_pdf_page_limit", 20),
    )
    config.ensure_directories()
    
    try:
        input_files = collect_cli_inputs(file_path=file_path, dir_path=dir_path)
    except (FileNotFoundError, NotADirectoryError, ValueError) as exc:
        logger.error(str(exc))
        sys.exit(1)

    if dir_path and not input_files:
        logger.info(
            "Keine verarbeitbaren Dokumente im angegebenen Ordner gefunden. "
            f"Unterstuetzte Dateitypen: {supported_suffixes_text()}."
        )
        return

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
        force_pipeline=force,
        redact_cloud_inputs=settings.get("redact_cloud_inputs", False),
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
        synology_enabled=settings.get("synology_enabled", False),
        synology_base_url=settings.get("synology_base_url", ""),
        synology_username=settings.get("synology_username", ""),
        synology_password=settings.get("synology_password", ""),
        synology_root_path=settings.get("synology_root_path", ""),
        synology_upload_pdf=settings.get("synology_upload_pdf", True),
        synology_upload_docx=settings.get("synology_upload_docx", False),
        synology_upload_json=settings.get("synology_upload_json", False),
        review_before_save=False,  # Im CLI-Modus immer unüberwacht verarbeiten
        large_pdf_reduced=settings.get("large_pdf_reduced", False),
        privacy_mode=settings.get("privacy_mode", "standard"),
        debug_artifacts_enabled=settings.get("debug_artifacts_enabled", True),
        ocr_languages=settings.get("ocr_languages", "deu+eng"),
        ocr_mode=settings.get("ocr_mode", "auto"),
    )
    
    # Verarbeitung starten
    if file_path:
        logger.info(f"Starte Verarbeitung für Einzeldatei: {input_files[0].name}")

    if dir_path:
        logger.info(f"Gefunden: {len(input_files)} Dokumente zur Verarbeitung.")

    processing_inputs = input_files if move_source else [stage_input_file(path, config) for path in input_files]
    if not move_source:
        logger.info("Quelldateien werden sicher in den Eingang kopiert; die angegebenen Originalpfade bleiben unverändert.")

    outcomes = []
    for idx, input_file in enumerate(processing_inputs, 1):
        if dir_path:
            logger.info(f"\n[{idx}/{len(processing_inputs)}] Verarbeite Datei: {input_file.name}")
        try:
            outcome = orchestrator.process_file(input_file)
            outcomes.append(outcome if isinstance(outcome, dict) else {"status": "completed"})
        except Exception as e:
            outcomes.append({"status": "failed", "source_name": input_file.name, "error": str(e)})
            logger.error(f"Fehler bei der Verarbeitung von {input_file.name}: {e}")

    if getattr(orchestrator, "deferred_organizations", None):
        orchestrator.process_deferred_organizations()
    failed = sum(1 for outcome in outcomes if outcome.get("status") == "failed")
    try:
        from core.local_store import LocalStore

        open_job_ids = {
            str(item.get("job_id") or "")
            for item in LocalStore(config).list_recoverable_work(limit=1000)
        }
        review_required = sum(
            1
            for outcome in outcomes
            if (
                outcome.get("review_required")
                and (
                    not outcome.get("job_id")
                    or str(outcome.get("job_id")) in open_job_ids
                )
            )
        )
    except Exception:
        review_required = sum(1 for outcome in outcomes if outcome.get("review_required"))
    completed = max(0, len(processing_inputs) - failed - review_required)
    result = {
        "attempted": len(processing_inputs),
        "completed": completed,
        "review_required": review_required,
        "failed": failed,
        "succeeded": completed,
        "outcomes": outcomes,
    }
    logger.info(
        "Batch abgeschlossen: %s vollständig, %s zur Prüfung, %s fehlgeschlagen.",
        completed,
        review_required,
        failed,
    )
    return result


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
        "--move-source",
        action="store_true",
        help="Verschiebt übergebene Quelldateien in das Archiv. Standardmäßig werden sie sicher kopiert.",
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
            result = run_cli(
                file_path=args.file,
                dir_path=args.dir,
                force=args.force,
                base_dir_override=args.base_dir,
                move_source=args.move_source,
            )
            if result and result.get("failed"):
                sys.exit(2)
            if result and result.get("review_required"):
                sys.exit(3)
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
