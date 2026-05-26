import shutil
import re
import uuid
import json
import logging
from pathlib import Path

from core.llm import LLMClient
from core.ocr import (
    run_image_to_pdf,
    run_ocrmypdf,
    run_docling_by_page_with_chunks,
    extract_pages_as_images,
    extract_ocr_text_per_page,
    inject_fused_text_and_metadata,
)
from core.docx_tools import save_markdown_as_docx
from core.config import AppConfig
from core.quality import QualityChecker
from core.audit import build_runtime_audit
from core.exporter import DocumentExporter
from core.document_extractors import (
    extract_text_from_doc,
    extract_text_from_docx,
    extract_text_from_odoc,
    extract_text_from_odt,
)
from core.job_history import JobHistory
from core.runtime_paths import normalize_token_path

logger = logging.getLogger("UnifiedOCR")

# Keywords für heuristische Tabellenerkennung
_TABULAR_KEYWORDS = (
    "abrechnung", "rechnung", "umsatz", "tabelle",
    "labor", "befund", "eur", "betrag", "gehalt", "lohn",
)


class PipelineOrchestrator:
    """
    Koordiniert alle Stufen der OCR-Pipeline für eine einzelne Datei.

    Ablauf:
        Stage 1  → Datei-Vorbereitung + OCRmyPDF
        Stage 2  → Docling + Seitenextraktion (PyMuPDF)
        Stage 2b → GLM-OCR (optionales spezialisiertes Dokumenten-OCR)
        Stage 3  → Vision-Review per Seite (Qwen3-VL o.ä.)
        Stage 4  → Text-Fusion per Seite
        Stage 5  → Qualitätskontrolle + Self-Correction Loop
        Stage 6  → Metadaten-Analyse
        Stage 7  → Export (PDF / TXT / DOCX)
    """

    def __init__(
        self,
        config: AppConfig,
        llm_client: LLMClient,
        output_format: str = "PDF und TXT",
        docx_mode: str = "Lesbare DOCX",
        log_callback=None,
        progress_callback=None,
        organize_enabled: bool = True,
        prompt_new_folder_callback=None,
        gdrive_enabled: bool = False,
        gdrive_token_path: str = "token.json",
        save_docx_enabled: bool = True,
        save_json_enabled: bool = True,
        gdrive_upload_pdf: bool = False,
        gdrive_upload_docx: bool = False,
        gdrive_upload_json: bool = False,
        review_before_save: bool = None,
        prompt_review_callback=None,
        on_processing_start_callback=None,
        large_pdf_reduced: bool = True,
    ):
        self.config = config
        self.llm = llm_client
        self.output_format = output_format
        self.docx_mode = docx_mode
        self.log_callback = log_callback
        self.progress_callback = progress_callback
        self.organize_enabled = organize_enabled
        self.prompt_new_folder_callback = prompt_new_folder_callback
        self.gdrive_enabled = gdrive_enabled
        self.gdrive_token_path = normalize_token_path(gdrive_token_path)
        self.save_docx_enabled = save_docx_enabled
        self.save_json_enabled = save_json_enabled
        self.gdrive_upload_pdf = gdrive_upload_pdf
        self.gdrive_upload_docx = gdrive_upload_docx
        self.gdrive_upload_json = gdrive_upload_json
        self.review_before_save = review_before_save
        self.prompt_review_callback = prompt_review_callback
        self.on_processing_start_callback = on_processing_start_callback
        self.large_pdf_reduced = large_pdf_reduced
        self._chosen_target_path = None
        self.deferred_organizations = []

    def log(self, message: str):
        if self.log_callback:
            self.log_callback(message)
        logger.info(message.strip())

    def report_progress(self, value: float):
        if self.progress_callback:
            self.progress_callback(value)

    # ------------------------------------------------------------------ #
    #  Stage 1: Datei-Vorbereitung & OCRmyPDF                             #
    # ------------------------------------------------------------------ #

    def _stage_prepare(self, original_path: Path, work_dir: Path) -> Path:
        """Konvertiert Bild → PDF oder kopiert PDF ins Arbeitsverzeichnis."""
        work_pdf = work_dir / f"{original_path.stem}_work.pdf"
        suffix = original_path.suffix.lower()
        if suffix in (".png", ".jpg", ".jpeg", ".heic"):
            self.log("Konvertiere Bild zu PDF...")
            if suffix == ".heic":
                try:
                    from PIL import Image
                    from pillow_heif import register_heif_opener
                    register_heif_opener()
                    img = Image.open(original_path)
                    if img.mode != "RGB":
                        img = img.convert("RGB")
                    img.save(work_pdf, "PDF", resolution=100.0, quality=90)
                    img.close()
                except Exception as e:
                    self.log(f"Fehler bei HEIC-zu-PDF Konvertierung: {e}")
                    raise RuntimeError(f"HEIC-zu-PDF Konvertierung fehlgeschlagen: {e}")
            else:
                run_image_to_pdf(original_path, work_pdf)
        else:
            shutil.copy2(original_path, work_pdf)
        return work_pdf

    def _stage_ocrmypdf(self, work_pdf: Path, work_dir: Path) -> tuple[Path, str]:
        """Führt OCRmyPDF aus und liest den Sidecar-Text."""
        self.log("Führe OCRmyPDF aus (--deskew)...")
        ocr_pdf = work_dir / "ocrmypdf_out.pdf"
        sidecar = work_dir / "ocrmypdf_text.txt"
        ocr_text = run_ocrmypdf(work_pdf, ocr_pdf, sidecar)
        return ocr_pdf, ocr_text

    def _extract_text_from_docx(self, docx_path: Path) -> str:
        return extract_text_from_docx(docx_path, self.log)

    def _extract_text_from_odt(self, odt_path: Path) -> str:
        return extract_text_from_odt(odt_path, self.log)

    def _extract_text_from_doc(self, doc_path: Path) -> str:
        return extract_text_from_doc(doc_path, self.log)

    def _extract_text_from_odoc(self, odoc_path: Path) -> str:
        return extract_text_from_odoc(odoc_path, self.log)

    # ------------------------------------------------------------------ #
    #  Stage 2: Docling & Seitenextraktion                                #
    # ------------------------------------------------------------------ #

    def _stage_docling(self, ocr_pdf: Path, work_pdf: Path) -> tuple[str, dict]:
        """Führt Docling mit HybridChunker aus, liefert seitenweises Markdown."""
        self.log("Führe Docling mit HybridChunker aus...")
        source = ocr_pdf if ocr_pdf.exists() else work_pdf
        try:
            return run_docling_by_page_with_chunks(source)
        except Exception as e:
            logger.exception("Fehler bei Docling")
            self.log(f"Docling Fehler: {e}")
            return "", {}

    def _stage_extract_pages(self, ocr_pdf: Path, work_dir: Path) -> tuple[list, dict]:
        """Rendert Seiten als PNG und extrahiert OCR-Text pro Seite (PyMuPDF)."""
        return extract_pages_as_images(ocr_pdf, work_dir), extract_ocr_text_per_page(ocr_pdf)

    def _detect_tabular(self, filename: str, page_markdowns: dict) -> bool:
        """Heuristik: Enthält das Dokument Tabellen oder Formulardaten?"""
        all_text = "\n".join(page_markdowns.values())
        if any(kw in filename.lower() for kw in _TABULAR_KEYWORDS):
            return True
        if any(kw in all_text.lower() for kw in _TABULAR_KEYWORDS):
            return True
        if len(all_text) > 0 and all_text.count("|") / len(all_text) > 0.005:
            return True
        return False

    # ------------------------------------------------------------------ #
    #  Stage 2b: GLM-OCR                                                 #
    # ------------------------------------------------------------------ #

    def _stage_glm_ocr(self, image_paths: list, total_pages: int) -> dict:
        """Führt das spezialisierte GLM-OCR Modell pro Seite aus."""
        if not self.llm.glm_ocr_model or self.llm.glm_ocr_model in ("Keins", "", "Kein GLM-OCR"):
            return {}

        self.log(f"Starte GLM-OCR ({self.llm.glm_ocr_model}) für {total_pages} Seiten...")
        results = {}
        for i, img_path in enumerate(image_paths):
            page_num = i + 1
            text = self.llm.run_glm_ocr(str(img_path), page_num)
            if text:
                results[page_num] = text
            self.report_progress(0.35 + 0.05 * (page_num / max(total_pages, 1)))

        self.log(f"GLM-OCR abgeschlossen ({len(results)} Seiten extrahiert).")
        return results

    # ------------------------------------------------------------------ #
    #  Stage 3: Vision-Review per Seite                                   #
    # ------------------------------------------------------------------ #

    def _stage_vision_review(
        self, image_paths: list, page_markdowns: dict, ocr_texts: dict, total_pages: int
    ) -> tuple[dict, dict]:
        """
        Vision-Modell prüft und korrigiert das Docling-Markdown anhand des Seitenbildes.
        Falls eine Seite kaum Text enthält, wird das Bild zusätzlich beschrieben.
        """
        if not self.llm.vision_model or self.llm.vision_model == "Keins":
            return {}, {}
        self.log(f"Starte Vision-Review ({self.llm.vision_model}) für {total_pages} Seiten...")
        results = {}
        page_descriptions = {}
        for i, img_path in enumerate(image_paths):
            page_num = i + 1
            self.log(f"  Vision-Review Seite {page_num}...")
            
            page_md = page_markdowns.get(page_num, "")
            page_ocr = ocr_texts.get(page_num, "")
            
            # Textdichte-Check: Haben wir sehr wenig Text?
            is_low_text = len(page_md.strip()) < 150 and len(page_ocr.strip()) < 150
            
            try:
                if is_low_text:
                    self.log(f"    -> Textarme Seite erkannt (Docling: {len(page_md.strip())} Z., OCR: {len(page_ocr.strip())} Z.). Rufe Bildbeschreibung ab...")
                    desc = self.llm.run_image_description(str(img_path), page_num)
                    if desc:
                        page_descriptions[page_num] = desc
                    
                    # Wenn gar kein Text da war, überspringen wir den normalen Review
                    if not page_md.strip() and not page_ocr.strip():
                        results[page_num] = ""
                    else:
                        results[page_num] = self.llm.run_vision_review(
                            str(img_path), page_md, page_num
                        )
                else:
                    results[page_num] = self.llm.run_vision_review(
                        str(img_path), page_md, page_num
                    )
            except Exception as e:
                logger.exception(f"Vision-Review/Bildbeschreibung fehlgeschlagen Seite {page_num}")
                self.log(str(e))
                results[page_num] = page_md
                
            self.report_progress(0.40 + 0.20 * (page_num / max(total_pages, 1)))
            
        self.log(f"Vision-Review abgeschlossen ({len(results)} Seiten verarbeitet).")
        return results, page_descriptions

    # ------------------------------------------------------------------ #
    #  Stage 4: Text-Fusion per Seite                                     #
    # ------------------------------------------------------------------ #

    def _best_page_text_source(self, *, vision_text: str = "", glm_text: str = "", docling_text: str = "", ocr_text: str = "") -> str:
        """Return the best non-empty page source when fusion is unavailable or degraded."""
        for text in (vision_text, glm_text, docling_text, ocr_text):
            if text and text.strip():
                return text
        return ""

    def _stage_fusion(
        self,
        image_paths: list,
        ocr_texts: dict,
        vision_markdowns: dict,
        page_markdowns: dict = None,
        glm_texts: dict = None,
        is_tabular: bool = False,
        total_pages: int = 1,
    ) -> dict:
        """Fusioniert alle Quellen (OCR-Sidecar, GLM-OCR, Vision) pro Seite."""
        self.log(f"Starte Text-Fusion ({self.llm.fusion_model}) für {total_pages} Seiten...")
        fused = {}

        for i, img_path in enumerate(image_paths):
            page_num = i + 1
            self.log(f"  Text-Fusion Seite {page_num}...")

            docling_text = page_markdowns.get(page_num, "") if page_markdowns else ""
            ocr_text = ocr_texts.get(page_num, "")
            vision_text = vision_markdowns.get(page_num, "")
            glm_text = glm_texts.get(page_num, "") if glm_texts else ""

            # Check if any text sources are present
            if not docling_text.strip() and not ocr_text.strip() and not vision_text.strip() and not glm_text.strip():
                self.log(f"  -> Überspringe LLM-Text-Fusion auf Seite {page_num} (kein Text in den Quellen).")
                fused[page_num] = ""
                self.report_progress(0.60 + 0.22 * (page_num / max(total_pages, 1)))
                continue

            try:
                page_fused = self.llm.run_page_fusion(
                    ocr_text=ocr_text,
                    intermediate_markdown=vision_markdowns.get(page_num, page_markdowns.get(page_num, "")) if page_markdowns else vision_markdowns.get(page_num, ""),
                    page_num=page_num,
                    previous_page_text=fused.get(page_num - 1, ""),
                    is_tabular=is_tabular,
                    glm_ocr_text=glm_texts.get(page_num, "") if glm_texts else ""
                )
                if page_fused and page_fused.strip():
                    fused[page_num] = page_fused
                else:
                    fused[page_num] = self._best_page_text_source(
                        vision_text=vision_text,
                        glm_text=glm_text,
                        docling_text=docling_text,
                        ocr_text=ocr_text,
                    )
                    self.log(f"  -> Text-Fusion Seite {page_num}: degraded fallback verwendet (leere/deaktivierte Fusion).")
            except Exception as e:
                self.log(f"  ⚠️ Fehler bei Text-Fusion Seite {page_num}: {e}")
                fused[page_num] = self._best_page_text_source(
                    vision_text=vision_text,
                    glm_text=glm_text,
                    docling_text=docling_text,
                    ocr_text=ocr_text,
                )

            self.report_progress(0.60 + 0.22 * (page_num / max(total_pages, 1)))

        self.log("Text-Fusion abgeschlossen.")
        return fused

    # ------------------------------------------------------------------ #
    #  Stage 5: Qualitätskontrolle & Self-Correction                     #
    # ------------------------------------------------------------------ #

    def _stage_quality(self, ocr_text: str, docling_text: str, vision_combined: str, fused_text: str) -> tuple[str, dict]:
        self.log("Führe Qualitätskontrolle durch...")
        report = QualityChecker.run_quality_check(ocr_text, docling_text, vision_combined, fused_text)
        missing = report.get("missing_values", [])
        
        if missing and self.llm.fusion_model and self.llm.fusion_model != "Keins":
            self.log(f"Automatische Nachkorrektur: {len(missing)} fehlende(r) Wert(e)...")
            missing_str = ", ".join(f"'{m['value']}' ({m['type']})" for m in missing)
            
            correction_sys = "Du bist ein präziser Text-Korrektor. Füge fehlende Werte an den logisch richtigen Stellen ein. Verändere sonst nichts am Dokument. Gib ausschließlich das korrigierte Dokument zurück."
            correction_user = f"Fusioniertes Dokument:\n\n```markdown\n{fused_text}\n```\n\nFehlende Werte (aus Qualitätskontrolle):\n{missing_str}\n\nFüge sie an der richtigen Stelle ein. Nur das korrigierte Dokument zurückgeben."
            
            try:
                corrected = self.llm.query(
                    model=self.llm.fusion_model,
                    system_prompt=correction_sys,
                    user_prompt=correction_user,
                    think=self.llm.think_fusion
                )
                if corrected and len(corrected) > len(fused_text) * 0.7:
                    fused_text = corrected
                    self.log("Nachkorrektur angewendet. Erneute Qualitätsprüfung...")
                    report = QualityChecker.run_quality_check(ocr_text, docling_text, vision_combined, fused_text)
            except Exception as e:
                logger.exception("Fehler bei Self-Correction")
                self.log(f"Nachkorrektur fehlgeschlagen: {e}")
                
        if report.get("warnings"):
            self.log(f"\n[WARNUNG] {len(report['warnings'])} Auffälligkeit(en):")
            for w in report["warnings"]:
                self.log(f"  ⚠️ {w}")
                logger.warning(w)
            return fused_text, report
        else:
            self.log("Qualitätskontrolle: Keine Auffälligkeiten.")
            return fused_text, report

    # ------------------------------------------------------------------ #
    #  Stage 6: Metadaten-Analyse                                         #
    # ------------------------------------------------------------------ #

    def _stage_analysis(self, fused_text: str) -> tuple[dict, str]:
        """Extrahiert Datum, Titel, Typ und Tags aus dem fusionierten Text."""
        self.log(f"Analysiere Metadaten ({self.llm.analysis_model})...")
        metadata = self.llm.run_analysis(fused_text)

        date = metadata.get("date", "")
        title = metadata.get("title", "")
        doc_type = metadata.get("document_type", "")

        metadata["subject"] = f"{title} - {doc_type}" if title else ""

        final_name = f"{date}_{title}_{doc_type}" if date and title else "dokument_searchable"
        final_name = re.sub(r'[\/*?:"<>|]', "", final_name)
        return metadata, final_name

    # ------------------------------------------------------------------ #
    #  Stage 7: Export                                                    #
    # ------------------------------------------------------------------ #

    def _stage_export(
        self,
        work_pdf: Path,
        fused_pages: dict,
        fused_text: str,
        final_name: str,
        metadata: dict,
        image_paths: list,
        quality_report: dict,
        is_docx: bool = False,
    ) -> tuple[Path, Path, Path]:
        exporter = DocumentExporter(
            config=self.config,
            output_format=self.output_format,
            docx_mode=self.docx_mode,
            save_docx_enabled=self.save_docx_enabled,
            save_json_enabled=self.save_json_enabled,
            gdrive_enabled=self.gdrive_enabled,
            gdrive_upload_docx=self.gdrive_upload_docx,
            gdrive_upload_json=self.gdrive_upload_json,
            log_callback=self.log,
            save_docx_func=save_markdown_as_docx,
            inject_pdf_func=inject_fused_text_and_metadata,
        )
        return exporter.export(
            work_pdf,
            fused_pages,
            fused_text,
            final_name,
            metadata,
            image_paths,
            quality_report,
            is_docx=is_docx,
        )

    def _resolve_exported_path(self, exported_paths: dict, key: str, moved_files: list | None = None) -> Path | None:
        """Resolve an exported artifact path after optional local organization."""
        if not isinstance(exported_paths, dict):
            return None
        path = exported_paths.get(key) if exported_paths else None
        path = Path(path) if path else None
        if path and path.exists():
            return path

        if path and moved_files:
            for moved in moved_files:
                moved_path = Path(moved)
                if moved_path.name == path.name and moved_path.exists():
                    return moved_path
        return path if path and path.exists() else None

    # ------------------------------------------------------------------ #
    #  Stage 8: Sortieren / Organize                                      #
    # ------------------------------------------------------------------ #

    def _stage_organize(
        self, fused_text: str, metadata: dict, final_name: str, is_docx: bool = False
    ) -> tuple[list, str]:
        from core.cloud.folder_registry import FolderRegistry
        from core.cloud.organizer import DocumentOrganizer

        self.log("Starte Dokumentensortierung...")
        try:
            # Registry laden
            registry = FolderRegistry(self.config.base_dir)
            known_paths = registry.get_known_paths()
            valid_persons = registry.get_persons()
            
            if hasattr(self, "_chosen_target_path") and self._chosen_target_path:
                target_path = self._chosen_target_path.strip().replace("\\", "/")
            else:
                # Klassifikation per LLM
                res = self.llm.run_classification(fused_text, metadata, known_paths, valid_persons)
                target_path = res.get("recommended_path", "Sonstiges")
            
            # Normalisierung und Validierung des Hauptordners
            parts = [p.strip() for p in target_path.replace("\\", "/").split("/") if p.strip()]
            if parts:
                matched_person = next((vp for vp in valid_persons if vp.lower() == parts[0].lower()), None)
                if matched_person:
                    parts[0] = matched_person
                else:
                    parts[0] = "Sonstiges"
                target_path = "/".join(parts)
            else:
                target_path = "Sonstiges"
                
            is_new = target_path not in known_paths
            self.log(f"  Empfohlener Pfad: '{target_path}' (Neu: {is_new})")
            
            if is_new:
                # Verschiebe die Dateien in einen temporären Staging-Ordner und stelle die Entscheidung zurück
                staging_dir = self.config.final_dir / "_staging" / final_name
                staging_dir.mkdir(parents=True, exist_ok=True)
                
                # Verschiebe Primärdateien (PDF, TXT, DOCX) aus final_dir nach staging_dir
                staged_files = []
                for item in self.config.final_dir.iterdir():
                    if item.is_file() and item.name.startswith(final_name) and item.suffix.lower() in (".pdf", ".txt", ".docx", ".odt", ".doc", ".odoc"):
                        dest = staging_dir / item.name
                        shutil.move(str(item), str(dest))
                        staged_files.append(str(dest))
                
                # Verschiebe auch Dateien aus dem begleitdateien-Ordner
                begleit_dir = self.config.final_dir / "begleitdateien"
                if begleit_dir.exists():
                    for item in begleit_dir.iterdir():
                        if item.is_file() and item.name.startswith(final_name):
                            dest = staging_dir / item.name
                            shutil.move(str(item), str(dest))
                            staged_files.append(str(dest))
                
                # In den deferred Queue packen
                self.deferred_organizations.append({
                    "final_name": final_name,
                    "proposed_path": target_path,
                    "staging_dir": staging_dir,
                    "fused_text": fused_text,
                    "metadata": metadata,
                    "is_docx": is_docx
                })
                self.log(f"  -> Einsortierung zurückgestellt. Dateien in Staging-Ordner verschoben.")
                return staged_files, target_path

            # Ermittle Dokumenttyp für automatische Unterordner-Konsolidierung
            doc_type = metadata.get("document_type", "").strip()
            parts = [p.strip() for p in target_path.split("/") if p.strip()]
            
            if len(parts) == 2 and doc_type:
                canonical_type = self._get_canonical_doc_type(doc_type)
                if canonical_type:
                    count = self._count_existing_documents_of_type(self.config.final_dir / target_path, canonical_type)
                    self.log(f"  Gefundene existierende Dokumente der Gruppe '{canonical_type}' in '{target_path}': {count}")
                    if count >= 1:
                        self.log(f"  -> Typ '{canonical_type}' kommt >= 1-mal vor. Nutze Unterordner '{target_path}/{canonical_type}' für diese Datei.")
                        self._consolidate_existing_documents(target_path, canonical_type)
                        self._consolidate_existing_documents_gdrive(target_path, canonical_type)
                        target_path = f"{target_path}/{canonical_type}"

            # Datei-Organizer ausführen
            organizer = DocumentOrganizer(self.config.final_dir)
            moved_files = organizer.organize(final_name, target_path)
            
            if moved_files:
                self.log(f"-> Einsortiert in Ordner: final/{target_path}")
            else:
                self.log("-> Keine Dateien zum Verschieben gefunden.")
                
            return moved_files, target_path
            
        except Exception as e:
            logger.exception("Fehler beim Sortieren des Dokuments")
            self.log(f"⚠️ Sortierung fehlgeschlagen: {e}")
            return [], "Sonstiges"

    def _get_canonical_doc_type(self, doc_type: str) -> str:
        dt = doc_type.strip().lower()
        if not dt:
            return ""
            
        synonyms = {
            "Lohnabrechnung": ["lohnabrechnung", "entgeldbescheinigung", "gehaltsabrechnung", "entgeltnachweis", "gehaltsnachweis", "entgelt"],
            "Rechnung": ["rechnung", "gebührenrechnung", "kaufbeleg", "quittung", "abrechnung"],
            "Befund": ["arztbrief", "befund", "patientenbrief", "befundbericht", "arztbefund"],
            "Lohnsteuerbescheinigung": ["lohnsteuerbescheinigung", "lohnsteuernachweis", "lohnsteuer"],
            "Versicherungspolice": ["versicherungspolice", "versicherungsnachweis", "police", "versicherungsvertrag"]
        }
        
        for canonical, list_of_syns in synonyms.items():
            if dt in list_of_syns or any(syn in dt for syn in list_of_syns):
                return canonical
                
        clean_name = re.sub(r'[\/*?:"<>|]', "", doc_type).strip()
        return clean_name.title() if clean_name else ""

    def _count_existing_documents_of_type(self, directory: Path, canonical_type: str) -> int:
        """
        Zählt existierende PDF-Dateien des Typs canonical_type (oder eines seiner Synonyme)
        in directory (rekursiv).
        """
        if not directory.exists():
            return 0
            
        synonyms = {
            "Lohnabrechnung": ["lohnabrechnung", "entgeldbescheinigung", "gehaltsabrechnung", "entgeltnachweis", "gehaltsnachweis", "entgelt"],
            "Rechnung": ["rechnung", "gebührenrechnung", "kaufbeleg", "quittung", "abrechnung"],
            "Befund": ["arztbrief", "befund", "patientenbrief", "befundbericht", "arztbefund"],
            "Lohnsteuerbescheinigung": ["lohnsteuerbescheinigung", "lohnsteuernachweis", "lohnsteuer"],
            "Versicherungspolice": ["versicherungspolice", "versicherungsnachweis", "police", "versicherungsvertrag"]
        }
        
        search_terms = {canonical_type.lower()}
        if canonical_type in synonyms:
            search_terms.update(synonyms[canonical_type])
            
        count = 0
        try:
            import fitz
        except ImportError:
            fitz = None
            
        for item in directory.rglob("*.pdf"):
            if not item.is_file():
                continue
                
            norm_name = re.sub(r"[^a-zA-Z0-9]", "", item.name).lower()
            if any(re.sub(r"[^a-zA-Z0-9]", "", term).lower() in norm_name for term in search_terms):
                count += 1
                continue
                
            if fitz:
                try:
                    doc = fitz.open(item)
                    subject = doc.metadata.get("subject", "").lower()
                    keywords = doc.metadata.get("keywords", "").lower()
                    doc.close()
                    if any(term.lower() in subject or term.lower() in keywords for term in search_terms):
                        count += 1
                except Exception:
                    pass
                    
        return count

    def _consolidate_existing_documents(self, parent_path: str, canonical_type: str):
        parent_dir = self.config.final_dir / parent_path
        sub_dir = parent_dir / canonical_type
        if not parent_dir.exists():
            return
            
        synonyms = {
            "Lohnabrechnung": ["lohnabrechnung", "entgeldbescheinigung", "gehaltsabrechnung", "entgeltnachweis", "gehaltsnachweis", "entgelt"],
            "Rechnung": ["rechnung", "gebührenrechnung", "kaufbeleg", "quittung", "abrechnung"],
            "Befund": ["arztbrief", "befund", "patientenbrief", "befundbericht", "arztbefund"],
            "Lohnsteuerbescheinigung": ["lohnsteuerbescheinigung", "lohnsteuernachweis", "lohnsteuer"],
            "Versicherungspolice": ["versicherungspolice", "versicherungsnachweis", "police", "versicherungsvertrag"]
        }
        
        search_terms = {canonical_type.lower()}
        if canonical_type in synonyms:
            search_terms.update(synonyms[canonical_type])
            
        sub_dir.mkdir(parents=True, exist_ok=True)
        
        for item in parent_dir.iterdir():
            if item.is_file() and item.suffix.lower() == ".pdf":
                norm_name = re.sub(r"[^a-zA-Z0-9]", "", item.name).lower()
                match_found = any(re.sub(r"[^a-zA-Z0-9]", "", term).lower() in norm_name for term in search_terms)
                
                if not match_found:
                    try:
                        import fitz
                        if fitz:
                            doc = fitz.open(item)
                            subject = doc.metadata.get("subject", "").lower()
                            keywords = doc.metadata.get("keywords", "").lower()
                            doc.close()
                            if any(term.lower() in subject or term.lower() in keywords for term in search_terms):
                                match_found = True
                    except Exception:
                        pass
                        
                if match_found:
                    dest_path = sub_dir / item.name
                    self.log(f"  Konsolidierung: Verschiebe '{item.name}' nach '{canonical_type}/'")
                    try:
                        shutil.move(str(item), str(dest_path))
                    except Exception as e:
                        logger.error(f"Fehler bei Konsolidierung von {item.name}: {e}")

    def _consolidate_existing_documents_gdrive(self, parent_path: str, canonical_type: str):
        """
        Verschiebt existierende Dokumente des Typs canonical_type (oder seiner Synonyme)
        auf Google Drive aus parent_path in parent_path/canonical_type.
        """
        if not self.gdrive_enabled:
            return
            
        try:
            from core.cloud.gdrive_client import GoogleDriveClient
            client = GoogleDriveClient()
            if not client.is_authenticated(self.gdrive_token_path):
                return
                
            service = client._get_service(self.gdrive_token_path)
            if not service:
                return
                
            synonyms = {
                "Lohnabrechnung": ["lohnabrechnung", "entgeldbescheinigung", "gehaltsabrechnung", "entgeltnachweis", "gehaltsnachweis", "entgelt"],
                "Rechnung": ["rechnung", "gebührenrechnung", "kaufbeleg", "quittung", "abrechnung"],
                "Befund": ["arztbrief", "befund", "patientenbrief", "befundbericht", "arztbefund"],
                "Lohnsteuerbescheinigung": ["lohnsteuerbescheinigung", "lohnsteuernachweis", "lohnsteuer"],
                "Versicherungspolice": ["versicherungspolice", "versicherungsnachweis", "police", "versicherungsvertrag"]
            }
            
            search_terms = {canonical_type.lower()}
            if canonical_type in synonyms:
                search_terms.update(synonyms[canonical_type])
                
            parent_folder_id = client._resolve_path_to_folder_id(service, parent_path)
            if parent_folder_id == "root":
                return
                
            subfolder_path = f"{parent_path}/{canonical_type}"
            subfolder_id = client._resolve_path_to_folder_id(service, subfolder_path)
            
            query = f"'{parent_folder_id}' in parents and mimeType != 'application/vnd.google-apps.folder' and trashed = false"
            results = service.files().list(q=query, fields="files(id, name, parents)").execute()
            files = results.get("files", [])
            
            for file in files:
                filename = file.get("name", "")
                norm_name = re.sub(r"[^a-zA-Z0-9]", "", filename).lower()
                match_found = any(re.sub(r"[^a-zA-Z0-9]", "", term).lower() in norm_name for term in search_terms)
                
                if match_found:
                    file_id = file.get("id")
                    current_parents = ",".join(file.get("parents", []))
                    self.log(f"  GDrive-Konsolidierung: Verschiebe '{filename}' nach '{canonical_type}/'...")
                    try:
                        service.files().update(
                            fileId=file_id,
                            addParents=subfolder_id,
                            removeParents=current_parents,
                            fields="id, parents"
                        ).execute()
                    except Exception as err:
                        logger.error(f"Fehler bei GDrive-Konsolidierung von {filename}: {err}")
        except Exception as e:
            logger.exception("Fehler bei der Google Drive Konsolidierung")
            self.log(f"⚠️ Google Drive Konsolidierung fehlgeschlagen: {e}")

    def _stage_gdrive_upload(self, pdf_file: Path, docx_file: Path, json_file: Path, target_path: str, is_docx_input: bool = False):
        """Uploads selected files to Google Drive, reproducing the local subdirectory layout."""
        if not self.gdrive_enabled:
            return
        
        self.log("Starte Google Drive Upload...")
        try:
            from core.cloud.gdrive_client import GoogleDriveClient
            client = GoogleDriveClient()
            if not client.is_authenticated(self.gdrive_token_path):
                self.log("⚠️ Google Drive Upload übersprungen: Nicht authentifiziert (token.json fehlt oder abgelaufen).")
                return

            upload_items = []
            if not is_docx_input and self.gdrive_upload_pdf and pdf_file and pdf_file.exists():
                upload_items.append(pdf_file)
            if (is_docx_input or self.gdrive_upload_docx) and docx_file and docx_file.exists():
                upload_items.append(docx_file)
            if self.gdrive_upload_json and json_file and json_file.exists():
                upload_items.append(json_file)

            if not upload_items:
                self.log("-> Keine Dateien für den Google Drive Upload ausgewählt oder vorhanden.")
                return

            for file_path in upload_items:
                p = Path(file_path)
                self.log(f"  Lade hoch: {p.name} nach Google Drive Ordner '{target_path}'...")
                try:
                    file_id = client.upload_file(self.gdrive_token_path, str(p), target_path)
                    self.log(f"  ✔ Erfolgreich hochgeladen: {p.name} (Google Drive ID: {file_id})")
                except Exception as upload_err:
                    self.log(f"  ⚠️ Fehler beim Upload von '{p.name}': {upload_err}")
                    logger.exception(f"Google Drive Upload-Fehler für '{p.name}'")
        except Exception as e:
            self.log(f"⚠️ Google Drive Integration fehlgeschlagen: {e}")
            logger.exception("Google Drive Integration Fehler")

    def process_deferred_organizations(self):
        """
        Arbeitet alle zurückgestellten Ordner-Einsortierungen ab, wenn der Watchdog im Leerlauf ist.
        """
        if not hasattr(self, "deferred_organizations") or not self.deferred_organizations:
            return
            
        self.log(f"\nVerarbeite {len(self.deferred_organizations)} zurückgestellte Ordner-Einsortierungen...")
        
        # Kopie erstellen, da wir Einträge während der Iteration entfernen
        deferred_list = list(self.deferred_organizations)
        self.deferred_organizations.clear()
        
        from core.cloud.folder_registry import FolderRegistry
        
        for item in deferred_list:
            final_name = item["final_name"]
            proposed_path = item["proposed_path"]
            staging_dir = item["staging_dir"]
            fused_text = item["fused_text"]
            metadata = item["metadata"]
            is_docx = item["is_docx"]
            
            self.log(f"\n--- Einsortierung für '{final_name}' ---")
            
            target_path = proposed_path
            try:
                registry = FolderRegistry(self.config.base_dir)
                known_paths = registry.get_known_paths()
                
                # Prüfen, ob der Pfad in der Zwischenzeit erstellt wurde
                if target_path in known_paths:
                    is_new = False
                else:
                    is_new = True
                    
                if is_new:
                    if self.prompt_new_folder_callback:
                        self.log(f"Warte auf Benutzerentscheidung für Ordner: '{target_path}'...")
                        chosen_path = self.prompt_new_folder_callback(target_path)
                        self.log(f"Benutzerentscheidung empfangen: '{chosen_path}'")
                        if chosen_path != target_path:
                            target_path = chosen_path
                
                # Normalisierung und Validierung des Hauptordners
                parts = [p.strip() for p in target_path.replace("\\", "/").split("/") if p.strip()]
                valid_persons = registry.get_persons()
                if parts:
                    matched_person = next((vp for vp in valid_persons if vp.lower() == parts[0].lower()), None)
                    if matched_person:
                        parts[0] = matched_person
                    else:
                        parts[0] = "Sonstiges"
                    target_path = "/".join(parts)
                else:
                    target_path = "Sonstiges"
                
                if target_path not in known_paths:
                    registry.add_path(target_path)
            except Exception as e:
                self.log(f"Fehler bei Ordner-Registry-Abgleich: {e}")
                target_path = "Sonstiges"

            # Verschiebe die Dateien aus dem Staging-Ordner an den finalen Zielort
            dest_dir = self.config.final_dir / target_path
            dest_dir.mkdir(parents=True, exist_ok=True)
            self.log(f"-> Einsortiert in Ordner: final/{target_path}")
            
            pdf_file = None
            docx_file = None
            for item_path in staging_dir.iterdir():
                if item_path.is_file():
                    dest_file = dest_dir / item_path.name
                    try:
                        shutil.move(str(item_path), str(dest_file))
                        if item_path.suffix.lower() == ".pdf":
                            pdf_file = dest_file
                        elif item_path.suffix.lower() == ".docx":
                            docx_file = dest_file
                    except Exception as move_err:
                        self.log(f"Fehler beim Verschieben von {item_path.name}: {move_err}")
                        logger.exception(f"Staging Move Fehler für {item_path.name}")
            
            # Staging-Verzeichnis für diese Datei löschen
            try:
                staging_dir.rmdir()
            except Exception:
                pass
            
            # Google Drive Upload
            if self.gdrive_enabled:
                json_file = self.config.final_dir / "begleitdateien" / f"{final_name}_quality_report.json"
                companion_docx = self.config.final_dir / "begleitdateien" / f"{final_name}.docx"
                
                gdrive_docx = docx_file if is_docx else companion_docx
                try:
                    self._stage_gdrive_upload(
                        pdf_file=pdf_file,
                        docx_file=gdrive_docx,
                        json_file=json_file,
                        target_path=target_path,
                        is_docx_input=is_docx
                    )
                except Exception as upload_err:
                    self.log(f"Fehler bei Google Drive Upload: {upload_err}")
            
            # Cleanup von ungenutzten Dateien
            if not self.save_docx_enabled:
                companion_docx = self.config.final_dir / "begleitdateien" / f"{final_name}.docx"
                if companion_docx.exists():
                    try:
                        companion_docx.unlink()
                        self.log("Lokale DOCX-Begleitdatei gelöscht (nach verzögertem Upload).")
                    except Exception as cleanup_err:
                        logger.warning(f"Konnte DOCX nicht löschen: {cleanup_err}")
            
            if not self.save_json_enabled:
                json_file = self.config.final_dir / "begleitdateien" / f"{final_name}_quality_report.json"
                if json_file.exists():
                    try:
                        json_file.unlink()
                        self.log("Lokale JSON-Begleitdatei gelöscht (nach verzögertem Upload).")
                    except Exception as cleanup_err:
                        logger.warning(f"Konnte JSON nicht löschen: {cleanup_err}")

        # Staging-Hauptordner löschen, falls leer
        staging_root = self.config.final_dir / "_staging"
        if staging_root.exists() and not any(staging_root.iterdir()):
            try:
                staging_root.rmdir()
            except Exception:
                pass
                
        begleit_dir = self.config.final_dir / "begleitdateien"
        if begleit_dir.exists() and not any(begleit_dir.iterdir()):
            try:
                begleit_dir.rmdir()
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    #  Haupt-Einstiegspunkt                                                #
    # ------------------------------------------------------------------ #

    def process_file(self, file_path: Path):
        self._chosen_target_path = None  # Prevent state leakage between subsequent runs
        filename = file_path.name
        metadata = {}
        final_name = ""
        target_path = ""
        job_history = JobHistory(self.config)
        job_id = job_history.start(file_path)
        self.log(f"\n{'─' * 50}")
        self.log(f"Starte Verarbeitung: {filename}")
        self.report_progress(0.05)

        # Datei ins original-Verzeichnis sichern
        original_path = self.config.original_dir / filename
        try:
            shutil.move(str(file_path), str(original_path))
        except Exception as e:
            self.log(f"Konnte Datei nicht verschieben: {e}")
            logger.exception(f"Datei-Move fehlgeschlagen: {filename}")
            job_history.finish(job_id, "failed", source_name=filename, error=str(e))
            return

        self.log(f"Datei nach '{self.config.original_dir.name}' verschoben.")

        if self.on_processing_start_callback:
            try:
                self.on_processing_start_callback(original_path)
            except Exception as e:
                logger.exception("Fehler in on_processing_start_callback")

        # Eindeutiger Ordner für Zwischenergebnisse
        work_dir = self.config.work_dir / f"work_{uuid.uuid4().hex}"
        work_dir.mkdir(parents=True, exist_ok=True)

        try:
            suffix = original_path.suffix.lower()
            is_docx = suffix in (".docx", ".odt", ".doc", ".odoc")

            if is_docx:
                self.log(f"Direkter Bypass-Modus für Office-Dokument ({suffix.upper()[1:]}) aktiv. Überspringe OCR und Seitenextraktion.")
                if suffix == ".docx":
                    ocr_text = self._extract_text_from_docx(original_path)
                elif suffix == ".odt":
                    ocr_text = self._extract_text_from_odt(original_path)
                elif suffix == ".doc":
                    ocr_text = self._extract_text_from_doc(original_path)
                elif suffix == ".odoc":
                    ocr_text = self._extract_text_from_odoc(original_path)
                
                fused_text = ocr_text
                fused_pages = {1: fused_text}
                image_paths = []
                quality_report = {}
                source_pdf_for_export = original_path
            else:
                # ── Stage 1: Vorbereitung & OCR ──────────────────────────────
                self.report_progress(0.10)
                work_pdf = self._stage_prepare(original_path, work_dir)
                self.report_progress(0.15)
                ocr_pdf, ocr_text = self._stage_ocrmypdf(work_pdf, work_dir)
                self.report_progress(0.30)

                # Seitenanzahl ermitteln
                total_pages = 1
                try:
                    import fitz
                    if fitz:
                        doc = fitz.open(ocr_pdf)
                        total_pages = len(doc)
                        doc.close()
                except Exception as e:
                    logger.warning(f"Konnte Seitenanzahl nicht per PyMuPDF ermitteln: {e}")

                # Reduzierte Analyse für große PDFs
                is_reduced = False
                if total_pages > self.config.large_pdf_page_limit and self.large_pdf_reduced:
                    is_reduced = True

                if is_reduced:
                    self.log(f"Großes Dokument erkannt ({total_pages} Seiten) und Modus 'Reduzierte Analyse' aktiv.")
                    self.log("Überspringe detaillierte Seitenverarbeitung (Docling, GLM-OCR, Vision, Fusion, QC).")
                    fused_text = ocr_text
                    fused_pages = {}
                    image_paths = []
                    quality_report = {
                        "warnings": [f"Reduzierte Analyse aktiv wegen hoher Seitenanzahl ({total_pages} > {self.config.large_pdf_page_limit})."]
                    }
                    source_pdf_for_export = ocr_pdf
                else:
                    # Detaillierte Analyse
                    # ── Stage 2: Docling & Seitenextraktion ───────────────────────
                    docling_text, page_mds = self._stage_docling(ocr_pdf, work_pdf)
                    self.report_progress(0.35)
                    image_paths, page_ocr_texts = self._stage_extract_pages(ocr_pdf, work_dir)
                    self.report_progress(0.40)

                    # ── Stage 2b & 3: GLM-OCR & Vision-Review ────────────────────
                    is_tabular = self._detect_tabular(filename, page_mds)
                    glm_texts = self._stage_glm_ocr(image_paths, total_pages)
                    vision_mds, page_descriptions = self._stage_vision_review(
                        image_paths, page_mds, page_ocr_texts, total_pages
                    )

                    # ── Stage 4: Fusion ──────────────────────────────────────────
                    fused_pages = self._stage_fusion(
                        image_paths, page_ocr_texts, vision_mds, page_mds, glm_texts, is_tabular, total_pages
                    )
                    
                    # ── Stage 5: Qualitätskontrolle ──────────────────────────────
                    self.report_progress(0.82)
                    vision_combined = "\n\n".join(vision_mds.values())
                    
                    initial_fused_text = "\n\n".join(fused_pages.values()) if fused_pages else ocr_text
                    fused_text_corrected, quality_report = self._stage_quality(ocr_text, docling_text, vision_combined, initial_fused_text)
                    source_pdf_for_export = ocr_pdf if ocr_pdf and Path(ocr_pdf).exists() else work_pdf
                    
                    if fused_text_corrected != initial_fused_text:
                        self.log("Qualitaets-Nachkorrektur hat Text veraendert. PDF-Textlayer bleibt seitenweise; korrigierter Dokumenttext wird fuer TXT/DOCX/Metadaten genutzt.")
                        fused_text = fused_text_corrected
                        desc_parts = []
                        for p_num, desc in sorted(page_descriptions.items()):
                            if total_pages > 1:
                                desc_parts.append(f"[Bildbeschreibung Seite {p_num}: {desc}]")
                            else:
                                desc_parts.append(f"[Bildbeschreibung: {desc}]")
                        
                        if desc_parts:
                            descriptions_combined = "\n\n".join(desc_parts)
                            if fused_text.strip():
                                fused_text = f"{descriptions_combined}\n\n{fused_text}"
                            else:
                                fused_text = descriptions_combined
                        for p_num, desc in page_descriptions.items():
                            orig_fused = fused_pages.get(p_num, "").strip()
                            if orig_fused:
                                fused_pages[p_num] = f"[Bildbeschreibung: {desc}]\n\n{orig_fused}"
                            else:
                                fused_pages[p_num] = f"[Bildbeschreibung: {desc}]"
                    else:
                        for p_num, desc in page_descriptions.items():
                            orig_fused = fused_pages.get(p_num, "").strip()
                            if orig_fused:
                                fused_pages[p_num] = f"[Bildbeschreibung: {desc}]\n\n{orig_fused}"
                            else:
                                fused_pages[p_num] = f"[Bildbeschreibung: {desc}]"
                        fused_text = "\n\n".join(fused_pages.values()) if fused_pages else ocr_text

            # ── Stage 6: Metadaten-Analyse ────────────────────────────────
            self.report_progress(0.88)
            metadata, final_name = self._stage_analysis(fused_text)
            self.report_progress(0.90)

            # ── Review Before Save ────────────────────────────────────────
            if self.review_before_save and self.prompt_review_callback:
                self.log("Warte auf Benutzerüberprüfung...")
                pre_target_path = "Sonstiges"
                if self.organize_enabled:
                    try:
                        from core.cloud.folder_registry import FolderRegistry
                        registry = FolderRegistry(self.config.base_dir)
                        known_paths = registry.get_known_paths()
                        res = self.llm.run_classification(fused_text, metadata, known_paths, registry.get_persons())
                        pre_target_path = res.get("recommended_path", "Sonstiges")
                    except Exception as e:
                        logger.exception("Pre-classification failed")

                review_res = self.prompt_review_callback(source_pdf_for_export, fused_text, metadata, pre_target_path)
                if review_res is None:
                    self.log("Benutzer hat die Verarbeitung abgebrochen.")
                    raise RuntimeError("Benutzer hat die Verarbeitung abgebrochen.")
                
                updated_fused_text, updated_metadata, custom_final_name, chosen_target_path = review_res
                fused_text = updated_fused_text
                metadata = updated_metadata
                self._chosen_target_path = chosen_target_path
                if custom_final_name:
                    final_name = re.sub(r'[\/*?:"<>|]', "", custom_final_name)
                else:
                    date      = metadata.get("date", "")
                    title     = metadata.get("title", "")
                    doc_type  = metadata.get("document_type", "")
                    metadata["subject"] = f"{title} - {doc_type}" if title else ""
                    final_name = f"{date}_{title}_{doc_type}" if date and title else "dokument_searchable"
                    final_name = re.sub(r'[\/*?:"<>|]', "", final_name)
                
                fused_pages = {1: fused_text}

            self.report_progress(0.92)

            # ── Stage 7: Export ───────────────────────────────────────────
            if isinstance(quality_report, dict):
                quality_report["runtime_audit"] = build_runtime_audit(
                    self.llm,
                    output_format=self.output_format,
                    docx_mode=self.docx_mode,
                    large_pdf_reduced=self.large_pdf_reduced,
                )
            exported_paths = self._stage_export(source_pdf_for_export, fused_pages, fused_text, final_name, metadata, image_paths, quality_report, is_docx=is_docx)
            if not isinstance(exported_paths, dict):
                begleit_dir = self.config.final_dir / "begleitdateien"
                exported_paths = {
                    "pdf": self.config.final_dir / f"{final_name}.pdf",
                    "txt": self.config.final_dir / f"{final_name}.txt",
                    "docx": (self.config.final_dir / f"{final_name}.docx") if is_docx else (begleit_dir / f"{final_name}.docx"),
                    "json": begleit_dir / f"{final_name}_quality_report.json",
                }

            # ── Stage 8: Sortieren / Organize ─────────────────────────────
            moved_files = []
            target_path = ""
            if self.organize_enabled:
                self.report_progress(0.96)
                moved_files, target_path = self._stage_organize(fused_text, metadata, final_name, is_docx=is_docx)
            
            # Prüfen, ob dieser Lauf zurückgestellt wurde
            is_deferred = False
            if hasattr(self, "deferred_organizations") and self.deferred_organizations:
                if self.deferred_organizations[-1]["final_name"] == final_name:
                    is_deferred = True

            # Die konkreten lokalen Dateipfade fuer Upload und Cleanup aus dem Export-Ergebnis bestimmen.
            pdf_file = self._resolve_exported_path(exported_paths, "pdf", moved_files) if not is_deferred else None
            docx_file = self._resolve_exported_path(exported_paths, "docx", moved_files) if not is_deferred else None
            json_file = self._resolve_exported_path(exported_paths, "json", moved_files) if not is_deferred else None

            # ── Google Drive Upload ───────────────────────────────────────
            if self.gdrive_enabled and not is_deferred:
                try:
                    self._stage_gdrive_upload(
                        pdf_file=pdf_file,
                        docx_file=docx_file,
                        json_file=json_file,
                        target_path=target_path,
                        is_docx_input=is_docx
                    )
                except Exception as upload_err:
                    self.log(f"Google Drive Upload-Fehler: {upload_err}")

            # Cleanup von ungenutzten Dateien
            if not is_deferred:
                try:
                    if docx_file and docx_file.exists() and not self.save_docx_enabled:
                        docx_file.unlink()
                except Exception as cleanup_err:
                    logger.warning(f"Konnte DOCX nicht löschen: {cleanup_err}")
                    
                if not self.save_json_enabled and json_file and json_file.exists():
                    try:
                        json_file.unlink()
                        self.log("Lokale JSON-Begleitdatei gelöscht (nur für Upload generiert).")
                    except Exception as cleanup_err:
                        logger.warning(f"Konnte JSON nicht löschen: {cleanup_err}")
            
            begleit_dir = self.config.final_dir / "begleitdateien"
            if begleit_dir.exists() and not any(begleit_dir.iterdir()):
                try:
                    begleit_dir.rmdir()
                except Exception:
                    pass

            title = metadata.get("title", "")
            tags  = metadata.get("tags", "")
            if title or tags:
                self.log(f"-> Titel: {title} | Typ: {metadata.get('document_type', '')} | Tags: {tags}")

            try:
                job_history.finish(
                    job_id,
                    "deferred" if is_deferred else "completed",
                    source_name=filename,
                    final_name=final_name,
                    target_path=target_path,
                    metadata=metadata,
                )
            except Exception as history_err:
                logger.warning(f"Job-Historie konnte nicht geschrieben werden: {history_err}")

            self.report_progress(1.0)
            self.log(f"{'─' * 50}\nVerarbeitung abgeschlossen: {filename}\n")

        except Exception as e:
            logger.exception(f"Schwerwiegender Fehler bei {filename}")
            self.log(f"FEHLER bei {filename}: {e}")
            try:
                job_history.finish(
                    job_id,
                    "failed",
                    source_name=filename,
                    final_name=final_name,
                    target_path=target_path,
                    error=str(e),
                    metadata=metadata,
                )
            except Exception as history_err:
                logger.warning(f"Job-Historie konnte nicht geschrieben werden: {history_err}")
            try:
                if original_path.exists():
                    shutil.move(str(original_path), str(self.config.error_dir / filename))
                self.config.cleanup_error_dir()
            except Exception:
                logger.exception("Konnte Fehlerdatei nicht verschieben")
            self.report_progress(0.0)

        finally:
            if work_dir.exists():
                try:
                    shutil.rmtree(work_dir)
                except Exception as e:
                    logger.warning(f"Arbeitsverzeichnis konnte nicht gelöscht werden: {e}")
