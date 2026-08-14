import shutil
import re
import json
import logging
import time
import inspect
from pathlib import Path

from core.llm import LLMClient
from core.ocr import (
    run_ocrmypdf,
    resolve_ocr_languages,
    inspect_pdf_page_content,
    run_docling_by_page_with_chunks,
    extract_pages_as_images,
    extract_ocr_text_per_page,
    extract_ordered_text_blocks_per_page,
    inject_fused_text_and_metadata,
    validate_archival_pdf,
)
from core.docx_tools import save_markdown_as_docx
from core.config import AppConfig
from core.quality import QualityChecker
from core.audit import build_runtime_audit
from core.exporter import DocumentExporter, write_quality_report_atomic
from core.document_extractors import (
    extract_text_from_doc,
    extract_text_from_docx,
    extract_text_from_odoc,
    extract_text_from_odt,
)
from core.job_context import ExtractionResult, JobContext
from core.job_history import JobHistory
from core.manifest import JobManifest
from core.diagnostics import DiagnosticsRecorder
from core.cache import sha256_file
from core.file_types import SUPPORTED_IMAGE_SUFFIXES, SUPPORTED_OFFICE_SUFFIXES
from core.filename_quality import usable_filename_title
from core.input_files import unique_path_for
from core.local_store import LocalStore
from core.maintenance import remove_directory_tree
from core.runtime_paths import normalize_token_path
from core.metadata import assess_metadata_evidence, metadata_tags_text, normalize_metadata
from core.cloud.folder_registry import UnsafeArchivePath, normalize_archive_path
from core.workflow_status import (
    build_expected_drive_members,
    make_workflow_event,
    summarize_google_drive_audit,
    summarize_synology_audit,
)

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
        Stage 5  → Evidenzbasierte Qualitätskontrolle + Review-Gate
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
        stage_callback=None,
        organize_enabled: bool = True,
        confirm_sorting_each_document: bool = False,
        prompt_new_folder_callback=None,
        prompt_sorting_callback=None,
        gdrive_enabled: bool = False,
        gdrive_token_path: str = "token.json",
        save_docx_enabled: bool = True,
        save_json_enabled: bool = True,
        gdrive_upload_pdf: bool = False,
        gdrive_upload_docx: bool = False,
        gdrive_upload_json: bool = False,
        synology_enabled: bool = False,
        synology_base_url: str = "",
        synology_username: str = "",
        synology_password: str = "",
        synology_root_path: str = "",
        synology_upload_pdf: bool = True,
        synology_upload_docx: bool = False,
        synology_upload_json: bool = False,
        review_before_save: bool = None,
        prompt_review_callback=None,
        on_processing_start_callback=None,
        large_pdf_reduced: bool = False,
        privacy_mode: str = "standard",
        debug_artifacts_enabled: bool = True,
        ocr_languages: str | tuple[str, ...] | list[str] = "deu+eng",
        ocr_mode: str = "auto",
    ):
        self.config = config
        self.llm = llm_client
        self.output_format = output_format
        self.docx_mode = docx_mode
        self.log_callback = log_callback
        self.progress_callback = progress_callback
        self.stage_callback = stage_callback
        self.organize_enabled = organize_enabled
        self.confirm_sorting_each_document = bool(confirm_sorting_each_document)
        self.prompt_new_folder_callback = prompt_new_folder_callback
        self.prompt_sorting_callback = prompt_sorting_callback
        self.privacy_mode = privacy_mode or "standard"
        self.gdrive_enabled = bool(gdrive_enabled) and self.privacy_mode != "local_only"
        self.gdrive_token_path = normalize_token_path(gdrive_token_path)
        self.save_docx_enabled = save_docx_enabled
        self.save_json_enabled = save_json_enabled
        self.gdrive_upload_pdf = gdrive_upload_pdf
        self.gdrive_upload_docx = gdrive_upload_docx
        self.gdrive_upload_json = gdrive_upload_json
        self.synology_base_url = (synology_base_url or "").strip()
        self.synology_username = (synology_username or "").strip()
        self.synology_password = synology_password or ""
        self.synology_root_path = (synology_root_path or "").strip().replace("\\", "/")
        self.synology_upload_pdf = synology_upload_pdf
        self.synology_upload_docx = synology_upload_docx
        self.synology_upload_json = synology_upload_json
        self.synology_enabled = bool(synology_enabled)
        if self.synology_enabled and self.privacy_mode == "local_only":
            try:
                from core.cloud.synology_client import is_private_webdav_url
                if not is_private_webdav_url(self.synology_base_url):
                    self.synology_enabled = False
                    self.log("Privacy Mode local_only: Synology/WebDAV deaktiviert, weil die URL nicht lokal wirkt.")
            except Exception:
                self.synology_enabled = False
        self.review_before_save = review_before_save
        self.prompt_review_callback = prompt_review_callback
        self.on_processing_start_callback = on_processing_start_callback
        self.large_pdf_reduced = large_pdf_reduced
        self.debug_artifacts_enabled = bool(debug_artifacts_enabled)
        self.ocr_languages = ocr_languages or "deu+eng"
        self.ocr_mode = str(ocr_mode or "auto").strip().lower()
        # Der gesamte Pro-Dokument-Zustand liegt in einem austauschbaren
        # Container. process_file ersetzt ihn zu Beginn jedes Laufs komplett.
        self._job = JobContext()
        self.deferred_organizations = []

    # ------------------------------------------------------------------ #
    #  Pro-Job-Zustand                                                     #
    # ------------------------------------------------------------------ #
    #  Die Namen bleiben die bisherigen, damit der Rumpf von process_file,
    #  die GUI und die Tests unveraendert weiterlaufen. Gehalten werden die
    #  Werte aber im JobContext, der pro Dokument neu erzeugt wird.

    def _begin_job(self, *, manifest_required: bool = False) -> JobContext:
        """Start a fresh per-document state. Nothing can leak from the last run."""
        self._job = JobContext(manifest_required=manifest_required)
        return self._job

    @property
    def _current_job_id(self) -> str:
        return self._job.job_id

    @_current_job_id.setter
    def _current_job_id(self, value) -> None:
        self._job.job_id = value

    @property
    def _current_source_name(self) -> str:
        return self._job.source_name

    @_current_source_name.setter
    def _current_source_name(self, value) -> None:
        self._job.source_name = value

    @property
    def _current_original_path(self):
        return self._job.original_path

    @_current_original_path.setter
    def _current_original_path(self, value) -> None:
        self._job.original_path = value

    @property
    def _current_manifest_required(self) -> bool:
        return self._job.manifest_required

    @_current_manifest_required.setter
    def _current_manifest_required(self, value) -> None:
        self._job.manifest_required = value

    @property
    def _current_organization_deferred(self) -> bool:
        return self._job.organization_deferred

    @_current_organization_deferred.setter
    def _current_organization_deferred(self, value) -> None:
        self._job.organization_deferred = value

    @property
    def _manual_review_completed(self) -> bool:
        return self._job.manual_review_completed

    @_manual_review_completed.setter
    def _manual_review_completed(self, value) -> None:
        self._job.manual_review_completed = value

    @property
    def _chosen_target_path(self):
        return self._job.chosen_target_path

    @_chosen_target_path.setter
    def _chosen_target_path(self, value) -> None:
        self._job.chosen_target_path = value

    @property
    def _active_workflow_step(self) -> str:
        return self._job.active_workflow_step

    @_active_workflow_step.setter
    def _active_workflow_step(self, value) -> None:
        self._job.active_workflow_step = value

    @property
    def _analysis_source_pages(self):
        return self._job.analysis_source_pages

    @_analysis_source_pages.setter
    def _analysis_source_pages(self, value) -> None:
        self._job.analysis_source_pages = value

    @property
    def _last_ocr_preflight(self) -> dict:
        return self._job.ocr_preflight

    @_last_ocr_preflight.setter
    def _last_ocr_preflight(self, value) -> None:
        self._job.ocr_preflight = value

    @property
    def _last_export_final_name(self) -> str:
        return self._job.export_final_name

    @_last_export_final_name.setter
    def _last_export_final_name(self, value) -> None:
        self._job.export_final_name = value

    @property
    def _rejected_filename_titles(self) -> list:
        return self._job.rejected_filename_titles

    @_rejected_filename_titles.setter
    def _rejected_filename_titles(self, value) -> None:
        self._job.rejected_filename_titles = value

    @property
    def _last_organize_audit(self) -> list:
        return self._job.organize_audit

    @_last_organize_audit.setter
    def _last_organize_audit(self, value) -> None:
        self._job.organize_audit = value

    @property
    def _last_google_drive_summary(self):
        return self._job.google_drive_summary

    @_last_google_drive_summary.setter
    def _last_google_drive_summary(self, value) -> None:
        self._job.google_drive_summary = value

    @property
    def _last_synology_summary(self):
        return self._job.synology_summary

    @_last_synology_summary.setter
    def _last_synology_summary(self, value) -> None:
        self._job.synology_summary = value

    def _call_compatible_callback(self, callback, *args):
        try:
            signature = inspect.signature(callback)
        except (TypeError, ValueError):
            return callback(*args)

        parameters = list(signature.parameters.values())
        if any(param.kind == inspect.Parameter.VAR_POSITIONAL for param in parameters):
            return callback(*args)

        positional_params = [
            param for param in parameters
            if param.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        ]
        accepted_count = len(positional_params)
        if len(args) > accepted_count:
            return callback(*args[:accepted_count])
        return callback(*args)

    def _is_external_model(self, model: str) -> bool:
        prefix = (model or "").split("/", 1)[0].lower()
        return prefix in {"openai", "gemini", "mistral", "anthropic", "cohere", "vertex_ai"}

    def _enforce_privacy_mode(self):
        if self.privacy_mode != "local_only":
            return
        for attr in ("vision_model", "fusion_model", "analysis_model", "glm_ocr_model"):
            model = getattr(self.llm, attr, "")
            if self._is_external_model(model):
                setattr(self.llm, attr, "Keins")
                self.log(f"Privacy Mode local_only: externes Modell deaktiviert ({model}).")

    def log(self, message: str):
        if self.log_callback:
            self.log_callback(message)
        logger.info(message.strip())

    def report_progress(self, value: float):
        if self.progress_callback:
            self.progress_callback(value)

    def report_workflow_status(
        self,
        step: str,
        state: str,
        message: str = "",
        *,
        details: dict | None = None,
    ) -> dict:
        """Emit one structured status event without risking the document job."""
        event = make_workflow_event(
            step,
            state,
            message,
            job_id=str(getattr(self, "_current_job_id", "") or ""),
            source_name=str(getattr(self, "_current_source_name", "") or ""),
            details=details,
        )
        if step != "job":
            if state == "running":
                self._active_workflow_step = step
            elif self._active_workflow_step == step:
                self._active_workflow_step = ""
        if self.stage_callback:
            try:
                self.stage_callback(event)
            except Exception:
                logger.exception("Workflow-Statuscallback ist fehlgeschlagen")
        return event

    def _report_google_drive_confirmation(
        self,
        uploads,
        expected_members: dict,
    ) -> dict:
        summary = summarize_google_drive_audit(uploads, expected_members)
        self._last_google_drive_summary = summary
        self.report_workflow_status(
            "google_drive",
            summary["state"],
            summary["message"],
            details=summary["details"],
        )
        return summary

    def _report_synology_confirmation(self, uploads, expected_count: int) -> dict:
        summary = summarize_synology_audit(uploads, expected_count)
        self._last_synology_summary = summary
        self.report_workflow_status(
            "synology",
            summary["state"],
            summary["message"],
            details=summary["details"],
        )
        return summary

    # ------------------------------------------------------------------ #
    #  Stage 1: Datei-Vorbereitung & OCRmyPDF                             #
    # ------------------------------------------------------------------ #

    def _convert_image_to_pdf(self, original_path: Path, output_pdf: Path, suffix: str) -> Path:
        try:
            from PIL import Image, ImageOps, ImageSequence
            if suffix in {".heic", ".heif"}:
                from pillow_heif import register_heif_opener
                register_heif_opener()
        except ImportError as exc:
            raise RuntimeError(f"Bildformat {suffix.upper()} kann nicht gelesen werden: {exc}") from exc

        frames = []
        try:
            with Image.open(original_path) as image:
                for frame in ImageSequence.Iterator(image):
                    page = ImageOps.exif_transpose(frame.copy())
                    if page.mode in ("RGBA", "LA") or (page.mode == "P" and "transparency" in page.info):
                        rgba = page.convert("RGBA")
                        background = Image.new("RGB", rgba.size, "white")
                        background.paste(rgba, mask=rgba.getchannel("A"))
                        rgba.close()
                        page.close()
                        page = background
                    elif page.mode != "RGB":
                        page = page.convert("RGB")
                    frames.append(page)

            if not frames:
                raise RuntimeError("Keine lesbare Bildseite gefunden.")

            first, rest = frames[0], frames[1:]
            first.save(
                output_pdf,
                "PDF",
                resolution=300.0,
                quality=95,
                save_all=bool(rest),
                append_images=rest,
            )
            return output_pdf
        except Exception as exc:
            raise RuntimeError(f"Bild-zu-PDF-Konvertierung fehlgeschlagen ({suffix}): {exc}") from exc
        finally:
            for frame in frames:
                try:
                    frame.close()
                except Exception:
                    pass

    def _stage_prepare(self, original_path: Path, work_dir: Path) -> Path:
        """Konvertiert Bild → PDF oder kopiert PDF ins Arbeitsverzeichnis."""
        work_pdf = work_dir / f"{original_path.stem}_work.pdf"
        suffix = original_path.suffix.lower()
        if suffix in SUPPORTED_IMAGE_SUFFIXES:
            self.log(f"Konvertiere Bild ({suffix.upper()[1:]}) zu PDF...")
            self._convert_image_to_pdf(original_path, work_pdf, suffix)
        else:
            shutil.copy2(original_path, work_pdf)
        return work_pdf

    def _stage_ocrmypdf(self, work_pdf: Path, work_dir: Path) -> tuple[Path, str]:
        """Führt OCRmyPDF aus und liest den Sidecar-Text."""
        preflight = resolve_ocr_languages(self.ocr_languages)
        preflight["mode"] = self.ocr_mode
        preflight["output_type"] = "pdfa-2"
        preflight["vision_render_dpi"] = 220
        preflight["review_required"] = False
        preflight["review_reasons"] = []
        if (
            preflight.get("missing")
            or preflight.get("fallback_used")
            or not preflight.get("detection_available", False)
        ):
            preflight["review_required"] = True
            preflight["review_reasons"].append(
                {
                    "code": "ocr_language_preflight_incomplete",
                    "severity": "warning",
                    "message": (
                        "Die angeforderten OCR-Sprachen waren nicht vollständig prüfbar oder verfügbar; "
                        "das Ergebnis muss vor Veröffentlichung kontrolliert werden."
                    ),
                    "requested": preflight.get("requested", []),
                    "effective": preflight.get("effective", []),
                    "missing": preflight.get("missing", []),
                }
            )
        page_content = inspect_pdf_page_content(work_pdf)
        preflight["page_content"] = page_content
        hybrid_pages = page_content.get("hybrid_pages", []) if isinstance(page_content, dict) else []
        if str(self.ocr_mode).casefold() == "auto" and hybrid_pages:
            preflight["review_required"] = True
            preflight["review_reasons"].append(
                {
                    "code": "hybrid_pdf_pages_skipped_by_auto_mode",
                    "severity": "warning",
                    "message": (
                        "Hybride PDF-Seiten enthalten Digitaltext und große Rasterbilder. "
                        "Der Auto-Modus bewahrt diese Seiten, kann aber Text im Bild übersehen."
                    ),
                    "pages": hybrid_pages,
                    "action": "OCR-Layer gegen das Original prüfen oder gezielt im Reparaturmodus erneut verarbeiten.",
                }
            )
            preflight["warnings"].append(
                "Hybride Seiten im OCR-Auto-Modus erkannt: "
                + ", ".join(str(page) for page in hybrid_pages)
            )
        self._last_ocr_preflight = preflight
        for warning in preflight.get("warnings", []):
            self.log(f"OCR-Preflight: {warning}")
        effective_languages = tuple(preflight.get("effective") or ["deu"])
        self.log(
            "Führe OCRmyPDF aus "
            f"(Modus {self.ocr_mode}, Sprachen {'+'.join(effective_languages)}, Deskew/Rotation aktiv)..."
        )
        ocr_pdf = work_dir / "ocrmypdf_out.pdf"
        sidecar = work_dir / "ocrmypdf_text.txt"
        sidecar_text = run_ocrmypdf(
            work_pdf,
            ocr_pdf,
            sidecar,
            mode=self.ocr_mode,
            languages=effective_languages,
        )
        embedded_pages = extract_ocr_text_per_page(ocr_pdf) if ocr_pdf.is_file() else {}
        embedded_text = "\n\n".join(
            text
            for _page, text in sorted((embedded_pages or {}).items())
            if str(text or "").strip()
        )
        skipped_pages: set[int] = set()
        for marker in re.findall(
            r"\[OCR skipped on page\(s\)\s+([^\]]+)\]",
            str(sidecar_text or ""),
            flags=re.IGNORECASE,
        ):
            skipped_pages.update(int(value) for value in re.findall(r"\d+", marker))
        clean_sidecar_text = re.sub(
            r"\[OCR skipped on page\(s\)\s+[^\]]+\]",
            "",
            str(sidecar_text or ""),
            flags=re.IGNORECASE,
        ).strip()
        unextractable_skipped_pages = sorted(
            page
            for page in skipped_pages
            # OCRmyPDF may skip an image-heavy page because of a single
            # digital page number or stray glyph.  Such a token is technically
            # extractable but not credible coverage of the scanned page.
            if sum(
                character.isalnum()
                for character in str((embedded_pages or {}).get(page) or "")
            ) < 3
        )
        if unextractable_skipped_pages:
            preflight["review_required"] = True
            reason = {
                "code": "skipped_pdf_text_not_extractable",
                "severity": "error",
                "message": (
                    "OCRmyPDF hat Seiten wegen vorhandenem Text übersprungen, deren Textlayer "
                    "anschließend nicht zuverlässig extrahiert werden konnte."
                ),
                "pages": unextractable_skipped_pages,
                "action": "Betroffene Seiten im Original prüfen und gegebenenfalls im Redo-/Reparaturmodus verarbeiten.",
            }
            preflight["review_reasons"].append(reason)
            preflight["warnings"].append(
                reason["message"] + " Seiten: "
                + ", ".join(str(page) for page in unextractable_skipped_pages)
            )
        preflight["skipped_pages"] = sorted(skipped_pages)
        preflight["unextractable_skipped_pages"] = unextractable_skipped_pages
        ocr_text = embedded_text or clean_sidecar_text
        self._last_ocr_preflight["sidecar_chars"] = len(sidecar_text or "")
        self._last_ocr_preflight["embedded_text_chars"] = len(embedded_text or "")
        self._last_ocr_preflight["text_source"] = "embedded_pdf" if embedded_text else "sidecar"
        if embedded_text and sidecar_text and "OCR skipped" in sidecar_text:
            self.log("OCRmyPDF hat vorhandenen Digitaltext bewahrt; vollständiger Text wurde aus dem Ergebnis-PDF gelesen.")
        return ocr_pdf, ocr_text

    def _extract_text_from_docx(self, docx_path: Path) -> str:
        return extract_text_from_docx(docx_path, self.log)

    def _extract_text_from_odt(self, odt_path: Path) -> str:
        return extract_text_from_odt(odt_path, self.log)

    def _extract_text_from_doc(self, doc_path: Path) -> str:
        return extract_text_from_doc(doc_path, self.log)

    def _extract_text_from_odoc(self, odoc_path: Path) -> str:
        return extract_text_from_odoc(odoc_path, self.log)

    def _extract_office_text(self, original_path: Path, suffix: str) -> str:
        """Dispatch Office text extraction by suffix.

        Ein neues Format in SUPPORTED_OFFICE_SUFFIXES ohne passenden Extraktor
        fuehrte hier frueher zu einer ungebundenen Variablen und damit zu einem
        NameError statt zu einer verstaendlichen Meldung.
        """
        extractors = {
            ".docx": self._extract_text_from_docx,
            ".odt": self._extract_text_from_odt,
            ".doc": self._extract_text_from_doc,
            ".odoc": self._extract_text_from_odoc,
        }
        extractor = extractors.get(suffix)
        if extractor is None:
            raise RuntimeError(
                f"Fuer das Office-Format {suffix} ist kein Textextraktor hinterlegt."
            )
        return extractor(original_path)

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

    def _summarize_layout_blocks(self, layout_blocks: dict[int, list[dict]]) -> dict:
        """Create a compact, auditable summary of page text packets."""
        summary = {"pages": 0, "blocks_total": 0, "blocks": {}}
        for page_num, blocks in (layout_blocks or {}).items():
            summary["pages"] += 1
            summary["blocks_total"] += len(blocks or [])
            summary["blocks"][str(page_num)] = [
                {
                    "order": block.get("reading_order", index),
                    "bbox": [
                        round(float(block.get("x0", 0)), 2),
                        round(float(block.get("y0", 0)), 2),
                        round(float(block.get("x1", 0)), 2),
                        round(float(block.get("y1", 0)), 2),
                    ],
                    "text_preview": (block.get("text", "") or "")[:160],
                }
                for index, block in enumerate(blocks or [])
            ]
        return summary

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
    #  Stage 5: Qualitätskontrolle & menschliche Freigabe                #
    # ------------------------------------------------------------------ #

    def _stage_quality(self, ocr_text: str, docling_text: str, vision_combined: str, fused_text: str) -> tuple[str, dict]:
        self.log("Führe Qualitätskontrolle durch...")
        report = QualityChecker.run_quality_check(ocr_text, docling_text, vision_combined, fused_text)
        self._merge_ocr_preflight_quality(report)
        # Quality findings are evidence for a human decision, never an
        # instruction to let an LLM invent or reinsert values.  In particular,
        # conflicting OCR sources can contain different amounts or dates.  The
        # previous self-correction loop treated every source value as true and
        # could therefore add a false value to an archival derivative.
        report.setdefault("automatic_correction_applied", False)
        report.setdefault("requires_review", report.get("quality_status") in {"review", "critical"})
                
        if report.get("warnings"):
            self.log(f"\n[WARNUNG] {len(report['warnings'])} Auffälligkeit(en):")
            for w in report["warnings"]:
                self.log(f"  ⚠️ {w}")
                logger.warning(w)
            return fused_text, report
        else:
            self.log("Qualitätskontrolle: Keine Auffälligkeiten.")
            return fused_text, report

    def _merge_ocr_preflight_quality(self, report: dict) -> dict:
        """Promote OCR preflight uncertainty into the publication review gate."""
        preflight = self._last_ocr_preflight if isinstance(self._last_ocr_preflight, dict) else {}
        reasons = preflight.get("review_reasons") if isinstance(preflight.get("review_reasons"), list) else []
        if not preflight.get("review_required") or not reasons:
            return report

        warnings = report.setdefault("warnings", [])
        review_reasons = report.setdefault("review_reasons", [])
        known_codes = {
            reason.get("code")
            for reason in review_reasons
            if isinstance(reason, dict)
        }
        for reason in reasons:
            if not isinstance(reason, dict):
                continue
            message = str(reason.get("message") or "OCR-Preflight muss geprüft werden.")
            if message not in warnings:
                warnings.append(message)
            if reason.get("code") not in known_codes:
                review_reasons.append(dict(reason))
                known_codes.add(reason.get("code"))

        if report.get("severity") in {None, "", "info"}:
            report["severity"] = "warning"
        report["quality_score"] = min(int(report.get("quality_score", 100)), 70)
        if report.get("quality_status") == "ok":
            report["quality_status"] = "review"
        report["requires_review"] = True
        report["review_required"] = True
        review = report.setdefault("review", {})
        review["required"] = True
        review.setdefault("blocking", False)
        review["reasons"] = review_reasons
        review["auto_correction_allowed"] = False
        report["ocr_preflight"] = preflight
        return report

    @staticmethod
    def _mark_review_deferred(quality_report: dict | None) -> dict:
        """Record a review that was closed without approval.

        Deferring is a safe outcome, never a failed job: the exported package
        goes to the durable queue and every remote upload stays blocked until
        it is confirmed.  An existing critical severity is preserved.
        """
        report = quality_report if isinstance(quality_report, dict) else {}

        deferred_warning = (
            "Die Prüfung vor dem Speichern wurde ohne Freigabe geschlossen; "
            "das Paket bleibt in der Review-Queue."
        )
        warnings = list(report.get("warnings") or [])
        if deferred_warning not in warnings:
            warnings.append(deferred_warning)

        reasons = list(report.get("review_reasons") or [])
        if not any(
            isinstance(reason, dict) and reason.get("code") == "manual_review_deferred"
            for reason in reasons
        ):
            reasons.append({
                "code": "manual_review_deferred",
                "severity": "warning",
                "message": deferred_warning,
                "action": "Dokument, Metadaten und Zielordner in der Review-Queue bestätigen.",
            })

        review_details = dict(report.get("review") or {})
        review_details.update({
            "required": True,
            "blocking": True,
            "auto_correction_allowed": False,
            "reasons": reasons,
        })

        report.update({
            "quality_status": (
                "critical"
                if str(report.get("quality_status") or "").lower() == "critical"
                else "review"
            ),
            "severity": (
                report.get("severity")
                if str(report.get("severity") or "").lower() == "error"
                else "warning"
            ),
            "warnings": warnings,
            "requires_review": True,
            "review_required": True,
            "review_reasons": reasons,
            "review": review_details,
        })
        return report

    def _build_reduced_analysis_report(self, total_pages: int) -> dict:
        """Quality report for the reduced large-document mode."""
        page_limit = self.config.large_pdf_page_limit
        review_reasons = [{
            "code": "reduced_large_document_analysis",
            "severity": "warning",
            "message": "Nicht alle Seiten wurden detailliert analysiert.",
            "action": "Stichproben und zentrale Dokumentwerte am Original prüfen.",
        }]
        return {
            "severity": "warning",
            "quality_status": "review",
            "quality_score": 70,
            "warnings": [
                f"Reduzierte Analyse aktiv wegen hoher Seitenanzahl ({total_pages} > {page_limit})."
            ],
            "requires_review": True,
            "review_required": True,
            "review_reasons": review_reasons,
            "review": {
                "required": True,
                "blocking": False,
                "auto_correction_allowed": False,
                "reasons": review_reasons,
            },
        }

    def _merge_filename_quality(self, report: dict) -> dict:
        """Promote a discarded, noise-looking filename title into the gate."""
        if not isinstance(report, dict):
            return report
        rejected = list(getattr(self, "_rejected_filename_titles", []) or [])
        if not rejected:
            return report

        warnings = report.setdefault("warnings", [])
        review_reasons = report.setdefault("review_reasons", [])
        known_codes = {
            reason.get("code")
            for reason in review_reasons
            if isinstance(reason, dict)
        }
        entry = rejected[0]
        message = (
            "Der vorgeschlagene Dateiname wirkte wie roher OCR-Auswurf und wurde verworfen: "
            f"{str(entry.get('title') or '')[:120]!r}."
        )
        if message not in warnings:
            warnings.append(message)
        if "filename_title_rejected" not in known_codes:
            review_reasons.append({
                "code": "filename_title_rejected",
                "severity": "warning",
                "message": message,
                "reasons": entry.get("reasons") or [],
                "action": "Dateinamen in der Review-Queue pruefen und bei Bedarf manuell setzen.",
            })

        if report.get("severity") in {None, "", "info"}:
            report["severity"] = "warning"
        if report.get("quality_status") in {None, "", "ok"}:
            report["quality_status"] = "review"
        report["requires_review"] = True
        report["review_required"] = True
        review = report.setdefault("review", {})
        review["required"] = True
        review.setdefault("blocking", False)
        review["reasons"] = review_reasons
        review["auto_correction_allowed"] = False
        report["rejected_filename_titles"] = rejected
        return report

    @staticmethod
    def _merge_metadata_evidence_quality(report: dict, metadata_evidence: dict) -> dict:
        """Promote unsupported machine metadata into the publication gate."""
        report = report if isinstance(report, dict) else {}
        metadata_evidence = metadata_evidence if isinstance(metadata_evidence, dict) else {}
        report["metadata_evidence"] = metadata_evidence
        if not metadata_evidence.get("requires_review"):
            return report

        warnings = report.setdefault("warnings", [])
        review_reasons = report.setdefault("review_reasons", [])
        known_codes = {
            reason.get("code")
            for reason in review_reasons
            if isinstance(reason, dict)
        }
        for warning in metadata_evidence.get("warnings") or []:
            warning = str(warning or "").strip()
            if warning and warning not in warnings:
                warnings.append(warning)
        for reason in metadata_evidence.get("review_reasons") or []:
            if not isinstance(reason, dict):
                continue
            if reason.get("code") not in known_codes:
                review_reasons.append(dict(reason))
                known_codes.add(reason.get("code"))

        if report.get("severity") in {None, "", "info"}:
            report["severity"] = "warning"
        try:
            report["quality_score"] = min(int(report.get("quality_score", 100)), 70)
        except (TypeError, ValueError):
            report["quality_score"] = 70
        if report.get("quality_status") in {None, "", "ok"}:
            report["quality_status"] = "review"
        report["requires_review"] = True
        report["review_required"] = True
        review = report.setdefault("review", {})
        review["required"] = True
        review.setdefault("blocking", False)
        review["reasons"] = review_reasons
        review["auto_correction_allowed"] = False
        return report

    # ------------------------------------------------------------------ #
    #  Stage 6: Metadaten-Analyse                                         #
    # ------------------------------------------------------------------ #

    def _stage_analysis(
        self,
        fused_text: str,
        source_pages: dict[int, str] | None = None,
    ) -> tuple[dict, str]:
        """Extrahiert Datum, Titel, Typ und Tags aus dem fusionierten Text."""
        self.log(f"Analysiere Metadaten ({self.llm.analysis_model})...")
        if source_pages is None:
            candidate_pages = getattr(self, "_analysis_source_pages", None)
            source_pages = candidate_pages if isinstance(candidate_pages, dict) else None
        metadata = normalize_metadata(
            self.llm.run_analysis(fused_text),
            source_text=fused_text,
            source_pages=source_pages,
        )
        rejected_titles: list = []
        final_name = self._final_name_from_metadata(metadata, rejected=rejected_titles)
        self._rejected_filename_titles = rejected_titles
        for entry in rejected_titles:
            self.log(
                f"  ⚠️ Vorgeschlagener Dateiname wirkt wie OCR-Auswurf und wurde verworfen: "
                f"{entry['title'][:80]!r} ({'; '.join(entry['reasons'])})"
            )
        return metadata, final_name

    @staticmethod
    def _final_name_from_metadata(metadata: dict, *, rejected: list | None = None) -> str:
        """Create a stable, user-readable and Windows-safe archive name.

        A title that is plainly OCR noise is dropped instead of being frozen
        into the archive name.  The caller receives the reasons via ``rejected``
        so the document can be flagged for review rather than silently renamed.
        """
        date_part = str(metadata.get("document_date") or "undatiert").strip()
        type_part = str(metadata.get("document_type") or "").strip()

        candidate = str(metadata.get("filename_title") or metadata.get("title") or "").strip()
        title_part, reasons = usable_filename_title(candidate)
        if candidate and not title_part:
            if rejected is not None:
                rejected.append({"title": candidate, "reasons": reasons})
            # Ohne brauchbaren Titel traegt der Dokumenttyp den Namen; erst
            # wenn auch der fehlt, wird auf "dokument" zurueckgefallen.
            title_part = "" if type_part else "dokument"
        elif not candidate:
            title_part = "" if type_part else "dokument"

        raw = "_".join(part for part in (date_part, title_part, type_part) if part)
        return PipelineOrchestrator._sanitize_final_name(raw)

    @staticmethod
    def _sanitize_final_name(raw: str) -> str:
        raw = re.sub(r'[\x00-\x1f\\/*?:"<>|]+', "_", raw)
        raw = re.sub(r"\s+", "_", raw)
        raw = re.sub(r"_+", "_", raw).strip(" ._")
        reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
        if not raw or raw.upper() in reserved:
            raw = "undatiert_dokument"
        return raw[:180].rstrip(" ._") or "undatiert_dokument"

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
            gdrive_enabled=self.gdrive_enabled or self.synology_enabled,
            gdrive_upload_docx=self.gdrive_upload_docx or self.synology_upload_docx,
            gdrive_upload_json=self.gdrive_upload_json or self.synology_upload_json,
            log_callback=self.log,
            save_docx_func=save_markdown_as_docx,
            inject_pdf_func=inject_fused_text_and_metadata,
            validate_archival_pdf_func=validate_archival_pdf,
            validate_archival_pdf_enabled=not is_docx,
        )
        created = exporter.export(
            work_pdf,
            fused_pages,
            fused_text,
            final_name,
            metadata,
            image_paths,
            quality_report,
            is_docx=is_docx,
        )
        self._last_export_final_name = exporter.last_final_name or final_name
        return created

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
                if (
                    moved_path.suffix.lower() == path.suffix.lower()
                    and moved_path.name.startswith(f"{path.stem}_conflict_")
                    and moved_path.exists()
                ):
                    return moved_path
        return path if path and path.exists() else None

    @staticmethod
    def _reconcile_pdf_postflight_artifacts(
        quality_report: dict | None,
        exported_paths: dict | None,
    ) -> bool:
        """Keep postflight provenance and its sidecar on the actual PDF path."""
        if not isinstance(quality_report, dict) or not isinstance(exported_paths, dict):
            return False
        postflight = quality_report.get("pdf_postflight")
        pdf_path = exported_paths.get("pdf")
        if not isinstance(postflight, dict) or not pdf_path or not Path(pdf_path).is_file():
            return False
        durable_path = str(Path(pdf_path))
        changed = str(postflight.get("path") or "") != durable_path
        postflight["path"] = durable_path
        sidecar = exported_paths.get("json")
        if sidecar and Path(sidecar).is_file():
            write_quality_report_atomic(Path(sidecar), quality_report)
            changed = True
        return changed

    @staticmethod
    def _upload_stage_status(entries: list[dict] | None, *, enabled: bool) -> str:
        if not enabled:
            return "skipped"
        entries = [entry for entry in (entries or []) if isinstance(entry, dict)]
        if any(str(entry.get("action", "")).lower() == "failed" for entry in entries):
            return "failed"
        if entries:
            return "ok"
        return "skipped"

    def _classification_needs_prompt(self, result: dict) -> bool:
        if not result:
            return False
        if result.get("review_required") or result.get("abstained"):
            return True
        if "auto_assign" in result and not bool(result.get("auto_assign")):
            return True
        if result.get("is_new"):
            return False
        candidates = result.get("candidates") or []
        confidence = int(result.get("confidence") or result.get("score") or 0)
        if confidence < 60:
            return True
        if len(candidates) >= 2:
            top = int(candidates[0].get("score") or 0)
            second = int(candidates[1].get("score") or 0)
            if top < 85 and top - second <= 8:
                return True
        return False

    def _quality_requires_human_review(self, quality_report: dict | None) -> bool:
        review = (quality_report or {}).get("review") or {}
        return bool(
            (quality_report or {}).get("requires_review")
            or (quality_report or {}).get("review_required")
            or review.get("required")
        ) and not bool(getattr(self, "_manual_review_completed", False))

    @staticmethod
    def _quality_workflow_state(quality_report: dict | None) -> str:
        report = quality_report if isinstance(quality_report, dict) else {}
        if str(report.get("quality_status") or "").lower() == "critical":
            return "error"
        if str(report.get("severity") or "").lower() == "error":
            return "error"
        if (
            str(report.get("quality_status") or "").lower() == "review"
            or report.get("requires_review")
            or report.get("review_required")
            or (report.get("review") or {}).get("required")
        ):
            return "warning"
        return "success"

    def _normalize_target_path(
        self,
        target_path: str,
        valid_persons: list[str],
        *,
        strict: bool = False,
    ) -> str:
        raw_original = str(target_path or "").strip()
        raw = raw_original.replace("\\", "/")
        if not raw:
            if strict:
                raise UnsafeArchivePath("Ein manuell bestätigter Zielpfad darf nicht leer sein.")
            return "Sonstiges"

        try:
            candidate = Path(raw_original).expanduser()
            if candidate.is_absolute():
                final_root = self.config.final_dir.resolve(strict=False)
                relative = candidate.resolve(strict=False).relative_to(final_root)
                raw = relative.as_posix()
        except ValueError:
            if strict:
                raise UnsafeArchivePath(
                    "Der manuell bestätigte Zielpfad liegt außerhalb des final-Ordners."
                )
            self.log("  Zielpfad liegt ausserhalb des final-Ordners; verwende 'Sonstiges'.")
            raw = "Sonstiges"
        except Exception:
            # Nicht stillschweigend uebergehen: ein unerwarteter Fehler laesst
            # den Rohpfad unveraendert, wodurch das Dokument woanders landen
            # kann als erwartet.
            logger.warning(
                "Zielpfad %r konnte nicht gegen den final-Ordner geprueft werden; "
                "verwende den Rohwert.",
                raw_original,
                exc_info=True,
            )

        try:
            raw = normalize_archive_path(raw, default="Sonstiges", max_depth=8)
        except UnsafeArchivePath as exc:
            if strict:
                raise UnsafeArchivePath(
                    f"Der manuell bestätigte Zielpfad ist unsicher: {exc}"
                ) from exc
            self.log(f"  Unsicherer Zielpfad verworfen ({exc}); verwende 'Sonstiges'.")
            raw = "Sonstiges"

        parts = [p.strip() for p in raw.replace("\\", "/").split("/") if p.strip()]
        if parts and parts[0].lower() == "final":
            parts = parts[1:]
        if not parts:
            if strict:
                raise UnsafeArchivePath("Der manuell bestätigte Zielpfad enthält keinen Ordner.")
            return "Sonstiges"

        matched_person = next((vp for vp in valid_persons if vp.lower() == parts[0].lower()), None)
        if matched_person:
            parts[0] = matched_person
        else:
            if strict:
                raise UnsafeArchivePath(
                    f"Unbekannte erste Ordnerebene im manuell bestätigten Zielpfad: {parts[0]}"
                )
            parts[0] = "Sonstiges"
        try:
            return normalize_archive_path("/".join(parts), default="Sonstiges", max_depth=8)
        except UnsafeArchivePath as exc:
            if strict:
                raise UnsafeArchivePath(
                    f"Der manuell bestätigte Zielpfad ist unsicher: {exc}"
                ) from exc
            return "Sonstiges"

    @staticmethod
    def _build_package_move_intent(
        artifacts: dict[str, Path],
        *,
        target_dir: Path,
        target_label: str,
    ) -> dict:
        """Return a restart-safe filesystem move journal entry."""
        return {
            "phase": "prepared",
            "target_label": target_label,
            "target_dir": str(Path(target_dir)),
            "artifacts": {
                role: {
                    "source": str(path),
                    "name": path.name,
                    "sha256": sha256_file(path),
                }
                for role, path in artifacts.items()
                if path.is_file()
            },
            "prepared_at_epoch": time.time(),
        }

    # ------------------------------------------------------------------ #
    #  Stage 8: Sortieren / Organize                                      #
    # ------------------------------------------------------------------ #

    def _stage_organize(
        self,
        fused_text: str,
        metadata: dict,
        final_name: str,
        is_docx: bool = False,
        preview_pdf_path: Path | None = None,
        artifact_paths: dict | None = None,
        quality_report: dict | None = None,
    ) -> tuple[list, str]:
        from core.cloud.folder_registry import FolderRegistry
        from core.cloud.classification_memory import ClassificationMemory
        from core.cloud.organizer import DocumentOrganizer

        self.log("Starte Dokumentensortierung...")
        self._current_organization_deferred = False
        try:
            # Registry laden
            registry = FolderRegistry(self.config.base_dir)
            known_paths = registry.get_known_paths()
            valid_persons = registry.get_persons()
            path_contexts = registry.get_path_contexts()
            memory = ClassificationMemory(self.config.base_dir)
            store = LocalStore(self.config)
            organizer = DocumentOrganizer(self.config.final_dir)
            self._last_organize_audit = []
            memory_candidates = memory.build_candidates(fused_text, metadata, known_paths)
            classification_result = {}
            learning_source = ""
            review_item_id = None
            unresolved_review = False
            user_confirmed = False
            confirmed_sorting_deferred = False
            current_job_id = getattr(self, "_current_job_id", "")
            modern_job = bool(current_job_id and store.get_job(current_job_id))
            quality_gate_required = self._quality_requires_human_review(quality_report)
            explicit_artifacts = {
                str(role): Path(path)
                for role, path in (artifact_paths or {}).items()
                if path and Path(path).is_file()
            }
            review_payload = {
                "classification": classification_result,
                "manifest_required": bool(
                    getattr(self, "_current_manifest_required", False)
                ),
                "preview_pdf_path": str(preview_pdf_path) if preview_pdf_path else "",
                "original_path": str(getattr(self, "_current_original_path", "") or ""),
                "quality_review_reasons": (quality_report or {}).get("review_reasons", []),
            }
            
            if hasattr(self, "_chosen_target_path") and self._chosen_target_path:
                target_path = self._chosen_target_path.strip().replace("\\", "/")
                learning_source = "manual_review"
                user_confirmed = True
            else:
                # Klassifikation per LLM
                classification_result = self.llm.run_classification(
                    fused_text,
                    metadata,
                    known_paths,
                    valid_persons,
                    path_contexts,
                    memory_candidates,
                )
                target_path = classification_result.get("recommended_path", "Sonstiges")
                confidence = classification_result.get("confidence", classification_result.get("score", 0))
                if classification_result.get("reason") == "context_match":
                    self.log(f"  Kontexttreffer: {target_path} (Score {confidence})")
                elif classification_result.get("reason") in {"memory", "fallback"} and memory_candidates:
                    self.log(f"  Lernspeicher-Vorschlag: {target_path} (Score {confidence})")

                needs_sorting_prompt = self.confirm_sorting_each_document or self._classification_needs_prompt(classification_result)
                if quality_gate_required:
                    # A sorting decision cannot approve uncertain OCR facts.
                    # Preserve the classifier suggestion, but require a real
                    # document review before publication or synchronization.
                    unresolved_review = True
                    review_item_id = store.add_review_item(
                        job_id=current_job_id,
                        kind="ocr_quality",
                        source_name=final_name,
                        proposed_path=target_path,
                        candidates=classification_result.get("candidates", []),
                        metadata=metadata,
                        payload={**review_payload, "classification": classification_result},
                        artifacts={key: str(value) for key, value in explicit_artifacts.items()},
                        quality=quality_report or {},
                    )
                    self.log("  OCR-Qualitätsprüfung erforderlich; Dokumentpaket wird nicht automatisch publiziert.")
                elif needs_sorting_prompt:
                    prompt_kind = "sorting_confirm" if self.confirm_sorting_each_document else "sorting_uncertain"
                    if self.confirm_sorting_each_document:
                        self.log(f"  Frage Benutzer nach Zielpfad (Vorschlag: {target_path}, Score {confidence})...")
                    else:
                        self.log(f"  Sortierung unsicher (Score {confidence}). Frage Benutzer nach Zielpfad...")
                    prompt_result = dict(classification_result or {})
                    prompt_result["requires_confirmation"] = bool(self.confirm_sorting_each_document)
                    review_item_id = store.add_review_item(
                        job_id=current_job_id,
                        kind=prompt_kind,
                        source_name=final_name,
                        proposed_path=target_path,
                        candidates=classification_result.get("candidates", []),
                        metadata=metadata,
                        payload={**review_payload, "classification": classification_result},
                        artifacts={key: str(value) for key, value in explicit_artifacts.items()},
                        quality=quality_report or {},
                    )
                    chosen_path = None
                    if self.prompt_sorting_callback:
                        chosen_path = self._call_compatible_callback(
                            self.prompt_sorting_callback,
                            prompt_result,
                            known_paths,
                            target_path,
                            preview_pdf_path,
                        )
                    if chosen_path:
                        target_path = chosen_path.strip().replace("\\", "/")
                        learning_source = "sorting_prompt"
                        user_confirmed = True
                        # A current-session confirmation is authoritative for
                        # the folder, but it must not close a durable review
                        # before the job manifest, document index and optional
                        # remote sync have committed.  Modern pipeline jobs are
                        # therefore staged and finalized by ReviewQueueService
                        # after the audit evidence has joined the package.
                        if modern_job:
                            unresolved_review = True
                            confirmed_sorting_deferred = True
                    else:
                        unresolved_review = True
            
            rejected_manual_target = ""
            try:
                target_path = self._normalize_target_path(
                    target_path,
                    valid_persons,
                    strict=user_confirmed,
                )
            except UnsafeArchivePath as exc:
                # A manual choice is authority only for the exact path the user
                # confirmed.  Never silently turn it into Sonstiges and publish
                # under a different archival context.
                rejected_manual_target = str(target_path or "")
                user_confirmed = False
                learning_source = ""
                unresolved_review = True
                classification_result = dict(classification_result or {})
                classification_result.update({
                    "decision": "review",
                    "auto_assign": False,
                    "review_required": True,
                    "invalid_manual_target": rejected_manual_target,
                    "path_validation_error": str(exc),
                })
                target_path = self._normalize_target_path("Sonstiges", valid_persons)
                self.log(
                    "  Manuell gewählter Zielpfad ist ungültig und wurde nicht veröffentlicht: "
                    f"{exc}. Das Paket bleibt zur Prüfung zurückgestellt."
                )
                
            is_new = target_path not in known_paths
            self.log(f"  Empfohlener Pfad: '{target_path}' (Neu: {is_new})")
            
            if (is_new and not user_confirmed) or unresolved_review:
                if review_item_id is None:
                    review_item_id = store.add_review_item(
                        job_id=current_job_id,
                        kind=(
                            "ocr_quality"
                            if quality_gate_required
                            else "new_path"
                            if is_new
                            else "sorting_uncertain"
                        ),
                        source_name=final_name,
                        proposed_path=target_path,
                        candidates=classification_result.get("candidates", []),
                        metadata=metadata,
                        payload={
                            **review_payload,
                            "classification": classification_result,
                            "rejected_manual_target": rejected_manual_target,
                        },
                        artifacts={key: str(value) for key, value in explicit_artifacts.items()},
                        quality=quality_report or {},
                    )

                staging_label = normalize_archive_path(
                    f"_staging/{current_job_id or final_name}",
                    max_depth=4,
                )
                staging_dir = self.config.final_dir / Path(*staging_label.split("/"))
                staging_payload = {
                    **review_payload,
                    "classification": classification_result,
                    "staging_dir": str(staging_dir),
                    "fused_text": fused_text,
                    "is_docx": bool(is_docx),
                    "docx_mode": self.docx_mode,
                }
                if explicit_artifacts:
                    staging_payload["move_intent"] = self._build_package_move_intent(
                        explicit_artifacts,
                        target_dir=staging_dir,
                        target_label=staging_label,
                    )
                # Persist the destination before the filesystem commit.  A
                # restart can then reconcile the package by path and hash even
                # if the process stops immediately after the move.
                store.update_review_item(
                    review_item_id,
                    status="pending",
                    artifacts={key: str(value) for key, value in explicit_artifacts.items()},
                    payload=staging_payload,
                    quality=quality_report or {},
                    metadata=metadata,
                )
                if artifact_paths is not None and explicit_artifacts:
                    staged_paths = organizer.organize_artifacts(
                        explicit_artifacts,
                        staging_label,
                        package_id=current_job_id or final_name,
                    )
                elif artifact_paths is None:
                    staged_paths = organizer.organize(final_name, staging_label)
                else:
                    raise FileNotFoundError("Keine exportierten Artefakte für das Review-Staging gefunden.")
                staged_files = [str(path) for path in staged_paths]
                staged_artifacts = (
                    {
                        role: str(path)
                        for role, path in zip(explicit_artifacts, staged_paths)
                    }
                    if explicit_artifacts
                    else {
                        f"artifact_{index + 1}": str(path)
                        for index, path in enumerate(staged_paths)
                    }
                )
                if isinstance(staging_payload.get("move_intent"), dict):
                    staging_payload["move_intent"] = {
                        **staging_payload["move_intent"],
                        "phase": "committed",
                        "destinations": staged_artifacts,
                        "committed_at_epoch": time.time(),
                    }
                store.update_review_item(
                    review_item_id,
                    status="staged",
                    artifacts=staged_artifacts,
                    payload=staging_payload,
                    quality=quality_report or {},
                    metadata=metadata,
                )

                # New-folder decisions can still be completed in the current
                # GUI session.  All other unresolved work remains safely in
                # the persistent review queue.
                if (
                    (is_new and self.prompt_new_folder_callback and not unresolved_review)
                    or confirmed_sorting_deferred
                ):
                    self.deferred_organizations.append({
                        "final_name": final_name,
                        "proposed_path": target_path,
                        "staging_dir": staging_dir,
                        "preview_pdf_path": next(
                            (Path(path) for path in staged_files if Path(path).suffix.lower() == ".pdf"),
                            preview_pdf_path,
                        ),
                        "fused_text": fused_text,
                        "metadata": metadata,
                        "is_docx": is_docx,
                        "classification_result": classification_result,
                        "review_item_id": review_item_id,
                    })
                self._last_organize_audit = list(organizer.last_audit)
                self._current_organization_deferred = True
                self.log(f"  -> Einsortierung zurückgestellt. Dateien in Staging-Ordner verschoben.")
                return staged_files, target_path

            # Existing archives are never reorganized implicitly.  Move only
            # the explicit package created for the current job.
            if artifact_paths is not None:
                if not explicit_artifacts:
                    raise FileNotFoundError("Keine exportierten Artefakte für die Ablage gefunden.")
                moved_files = organizer.organize_artifacts(
                    explicit_artifacts,
                    target_path,
                    package_id=current_job_id or final_name,
                )
            else:
                moved_files = organizer.organize(final_name, target_path)
            self._last_organize_audit = list(organizer.last_audit)
            
            if moved_files:
                self.log(f"-> Einsortiert in Ordner: final/{target_path}")
                if is_new:
                    registry.add_path(target_path)
                    self.log(f"  Neuer Pfad registriert: '{target_path}'")
                if any(entry.get("action") in {"name_conflict", "moved_with_conflict_name"} for entry in self._last_organize_audit):
                    self.log("  Hinweis: Namenskonflikt erkannt; bestehende Datei wurde nicht überschrieben.")
                if review_item_id:
                    store.resolve_review_item(
                        review_item_id,
                        target_path,
                        metadata=metadata,
                        artifacts={
                            f"artifact_{index + 1}": str(path)
                            for index, path in enumerate(moved_files)
                        },
                        quality=quality_report or {},
                    )
                if learning_source and user_confirmed:
                    memory.record_decision(
                        chosen_path=target_path,
                        fused_text=fused_text,
                        metadata=metadata,
                        proposed_path=classification_result.get("recommended_path", ""),
                        candidates=classification_result.get("candidates", []),
                        source=learning_source,
                        confirmed=True,
                    )
            else:
                self.log("-> Keine Dateien zum Verschieben gefunden.")
                
            return moved_files, target_path
            
        except Exception as e:
            logger.exception("Fehler beim Sortieren des Dokuments")
            self.log(f"⚠️ Sortierung fehlgeschlagen: {e}")
            raise

    def _stage_unsorted_quality_review(
        self,
        *,
        fused_text: str,
        metadata: dict,
        final_name: str,
        artifact_paths: dict,
        quality_report: dict,
        preview_pdf_path: Path | None,
        is_docx: bool,
    ) -> list[Path]:
        """Stage a quality-blocked package when folder organization is disabled."""
        from core.cloud.organizer import DocumentOrganizer

        current_job_id = getattr(self, "_current_job_id", "")
        artifacts = {
            str(role): Path(path)
            for role, path in (artifact_paths or {}).items()
            if path and Path(path).is_file()
        }
        if not artifacts:
            raise FileNotFoundError("Keine exportierten Artefakte für das Qualitätsreview gefunden.")
        store = LocalStore(self.config)
        review_item_id = store.add_review_item(
            job_id=current_job_id,
            kind="ocr_quality",
            source_name=final_name,
            proposed_path="__archive_root__",
            candidates=[],
            metadata=metadata,
            payload={
                "classification": {},
                "organize_enabled": False,
                "manifest_required": bool(
                    getattr(self, "_current_manifest_required", False)
                ),
                "preview_pdf_path": str(preview_pdf_path) if preview_pdf_path else "",
                "original_path": str(getattr(self, "_current_original_path", "") or ""),
                "quality_review_reasons": quality_report.get("review_reasons", []),
            },
            artifacts={key: str(value) for key, value in artifacts.items()},
            quality=quality_report,
        )
        staging_label = normalize_archive_path(
            f"_staging/{current_job_id or final_name}",
            max_depth=4,
        )
        staging_dir = self.config.final_dir.joinpath(*staging_label.split("/"))
        staging_payload = {
            "classification": {},
            "organize_enabled": False,
            "manifest_required": bool(
                getattr(self, "_current_manifest_required", False)
            ),
            "preview_pdf_path": str(preview_pdf_path) if preview_pdf_path else "",
            "original_path": str(getattr(self, "_current_original_path", "") or ""),
            "quality_review_reasons": quality_report.get("review_reasons", []),
            "staging_dir": str(staging_dir),
            "fused_text": fused_text,
            "is_docx": bool(is_docx),
            "docx_mode": self.docx_mode,
            "move_intent": self._build_package_move_intent(
                artifacts,
                target_dir=staging_dir,
                target_label=staging_label,
            ),
        }
        store.update_review_item(
            review_item_id,
            status="pending",
            artifacts={role: str(path) for role, path in artifacts.items()},
            payload=staging_payload,
            metadata=metadata,
            quality=quality_report,
        )
        organizer = DocumentOrganizer(self.config.final_dir)
        staged_paths = organizer.organize_artifacts(
            artifacts,
            staging_label,
            package_id=current_job_id or final_name,
        )
        staged_artifacts = {
            role: str(path)
            for role, path in zip(artifacts, staged_paths)
        }
        staging_payload["preview_pdf_path"] = str(
            next((path for path in staged_paths if path.suffix.lower() == ".pdf"), preview_pdf_path or "")
        )
        staging_payload["move_intent"] = {
            **staging_payload["move_intent"],
            "phase": "committed",
            "destinations": staged_artifacts,
            "committed_at_epoch": time.time(),
        }
        store.update_review_item(
            review_item_id,
            status="staged",
            artifacts=staged_artifacts,
            payload=staging_payload,
            metadata=metadata,
            quality=quality_report,
        )
        self._last_organize_audit = list(organizer.last_audit)
        self._current_organization_deferred = True
        self.log("OCR-Qualitätsprüfung erforderlich; unsortiertes Paket sicher zurückgestellt.")
        return staged_paths

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
                    with fitz.open(item) as doc:
                        info = doc.metadata or {}
                        subject = str(info.get("subject") or "").lower()
                        keywords = str(info.get("keywords") or "").lower()
                    if any(term.lower() in subject or term.lower() in keywords for term in search_terms):
                        count += 1
                except Exception:
                    # Ohne Hinweis wirkt das Dokument spaeter einfach als nicht
                    # vorhanden, obwohl nur seine Metadaten unlesbar waren.
                    logger.debug(
                        "PDF-Metadaten von %s konnten nicht gelesen werden.",
                        item,
                        exc_info=True,
                    )
                    
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
                            # Handle vor dem folgenden shutil.move schliessen:
                            # eine offene PDF laesst sich unter Windows nicht
                            # verschieben.
                            with fitz.open(item) as doc:
                                info = doc.metadata or {}
                                subject = str(info.get("subject") or "").lower()
                                keywords = str(info.get("keywords") or "").lower()
                            if any(term.lower() in subject or term.lower() in keywords for term in search_terms):
                                match_found = True
                    except Exception:
                        logger.debug(
                            "PDF-Metadaten von %s konnten fuer die Konsolidierung "
                            "nicht gelesen werden.",
                            item,
                            exc_info=True,
                        )
                        
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
        uploads = []
        expected_members = {}
        self._last_google_drive_summary = None
        if not self.gdrive_enabled:
            self.report_workflow_status(
                "google_drive",
                "skipped",
                "Google Drive ist für diesen Auftrag nicht aktiviert.",
            )
            return uploads

        self.report_workflow_status(
            "google_drive",
            "running",
            "Google-Drive-Paket wird hochgeladen und anschließend geprüft.",
        )
        self.log("Starte Google Drive Upload...")
        try:
            from core.cloud.gdrive_client import GoogleDriveClient
            client = GoogleDriveClient()
            if not client.is_authenticated(self.gdrive_token_path):
                self.log("⚠️ Google Drive Upload übersprungen: Nicht authentifiziert (token.json fehlt oder abgelaufen).")
                uploads.append({
                    "provider": "google_drive",
                    "folder_path": target_path,
                    "action": "failed",
                    "error": "not_authenticated",
                })
                self.report_workflow_status(
                    "google_drive",
                    "error",
                    "Google Drive nicht bestätigt: Anmeldung fehlt oder ist abgelaufen.",
                    details={"expected_count": 0, "confirmed_count": 0, "error": "not_authenticated"},
                )
                return uploads

            upload_items = []
            if not is_docx_input and self.gdrive_upload_pdf and pdf_file and pdf_file.exists():
                upload_items.append(pdf_file)
            if (is_docx_input or self.gdrive_upload_docx) and docx_file and docx_file.exists():
                upload_items.append(docx_file)
            if self.gdrive_upload_json and json_file and json_file.exists():
                upload_items.append(json_file)

            if not upload_items:
                self.log("-> Keine Dateien für den Google Drive Upload ausgewählt oder vorhanden.")
                self._report_google_drive_confirmation([], {})
                return uploads

            package_items = {}
            for p in upload_items:
                role = (
                    "pdf" if p == pdf_file
                    else "docx" if p == docx_file
                    else "quality_report" if p == json_file
                    else p.suffix.lstrip(".")
                )
                package_items[role] = p
            expected_members = build_expected_drive_members(package_items)

            if hasattr(client.__class__, "upload_package_with_audit"):
                from core.cloud.folder_registry import FolderRegistry

                known_ids = FolderRegistry(self.config.base_dir).get_drive_folder_map()
                self.log(
                    f"  Plane archivfestes Drive-Paket mit {len(package_items)} Datei(en) "
                    f"im Ordner '{target_path}'..."
                )
                package_audit = client.upload_package_with_audit(
                    self.gdrive_token_path,
                    package_items,
                    target_path,
                    known_ids=known_ids,
                )
                if not isinstance(package_audit, list):
                    raise RuntimeError("Google Drive lieferte kein gültiges Paket-Audit.")
                uploads.extend(package_audit)
                for entry in package_audit:
                    self.log(
                        f"  ✔ Google Drive: {entry.get('remote_filename') or entry.get('filename')} "
                        f"{entry.get('action', 'uploaded')} "
                        f"(ID: {entry.get('drive_file_id')})"
                    )
                self._report_google_drive_confirmation(uploads, expected_members)
                return uploads

            for file_path in upload_items:
                p = Path(file_path)
                self.log(f"  Lade hoch: {p.name} nach Google Drive Ordner '{target_path}'...")
                try:
                    if hasattr(client.__class__, "upload_file_with_audit"):
                        upload_audit = client.upload_file_with_audit(self.gdrive_token_path, str(p), target_path)
                    else:
                        file_id = client.upload_file(self.gdrive_token_path, str(p), target_path)
                        upload_audit = {
                            "provider": "google_drive",
                            "local_path": str(p),
                            "filename": p.name,
                            "drive_file_id": file_id,
                            "folder_path": target_path,
                            "action": "uploaded",
                        }
                    if not isinstance(upload_audit, dict):
                        upload_audit = {
                            "provider": "google_drive",
                            "local_path": str(p),
                            "filename": p.name,
                            "drive_file_id": str(upload_audit),
                            "folder_path": target_path,
                            "action": "uploaded",
                        }
                    upload_audit.setdefault("provider", "google_drive")
                    upload_audit.setdefault("local_path", str(p))
                    upload_audit.setdefault("filename", p.name)
                    upload_audit.setdefault("folder_path", target_path)
                    uploads.append(upload_audit)
                    self.log(
                        f"  ✔ Google Drive: {p.name} {upload_audit.get('action', 'uploaded')} "
                        f"(ID: {upload_audit.get('drive_file_id')})"
                    )
                except Exception as upload_err:
                    self.log(f"  ⚠️ Fehler beim Upload von '{p.name}': {upload_err}")
                    logger.exception(f"Google Drive Upload-Fehler für '{p.name}'")
                    uploads.append({
                        "provider": "google_drive",
                        "local_path": str(p),
                        "filename": p.name,
                        "folder_path": target_path,
                        "action": "failed",
                        "error": str(upload_err),
                    })
            self._report_google_drive_confirmation(uploads, expected_members)
        except Exception as e:
            self.log(f"⚠️ Google Drive Integration fehlgeschlagen: {e}")
            logger.exception("Google Drive Integration Fehler")
            uploads.append({
                "provider": "google_drive",
                "folder_path": target_path,
                "action": "failed",
                "error": str(e),
            })
            if expected_members:
                self._report_google_drive_confirmation(uploads, expected_members)
            else:
                self.report_workflow_status(
                    "google_drive",
                    "error",
                    f"Google Drive nicht bestätigt: {e}",
                    details={"expected_count": 0, "confirmed_count": 0, "error": str(e)},
                )
        return uploads

    def _stage_synology_upload(self, pdf_file: Path, docx_file: Path, json_file: Path, target_path: str, is_docx_input: bool = False):
        """Uploads selected files to a Synology WebDAV target, preserving the local folder layout."""
        uploads = []
        expected_count = 0
        self._last_synology_summary = None
        if not self.synology_enabled:
            self.report_workflow_status(
                "synology",
                "skipped",
                "Synology/NAS ist für diesen Auftrag nicht aktiviert.",
            )
            return uploads

        self.report_workflow_status(
            "synology",
            "running",
            "Synology-/NAS-Paket wird übertragen und geprüft.",
        )
        self.log("Starte Synology/WebDAV Upload...")
        try:
            from core.cloud.synology_client import SynologyWebDAVClient

            client = SynologyWebDAVClient(
                base_url=self.synology_base_url,
                username=self.synology_username,
                password=self.synology_password,
                root_path=self.synology_root_path,
            )
            if not client.is_configured:
                self.log("⚠️ Synology/WebDAV Upload übersprungen: Server, Benutzername oder Passwort fehlen.")
                uploads.append({
                    "provider": "synology_webdav",
                    "folder_path": target_path,
                    "action": "failed",
                    "error": "not_configured",
                })
                self.report_workflow_status(
                    "synology",
                    "error",
                    "Synology/NAS nicht bestätigt: Verbindung ist unvollständig konfiguriert.",
                    details={"expected_count": 0, "confirmed_count": 0, "error": "not_configured"},
                )
                return uploads

            upload_items = []
            if not is_docx_input and self.synology_upload_pdf and pdf_file and pdf_file.exists():
                upload_items.append(pdf_file)
            if (is_docx_input or self.synology_upload_docx) and docx_file and docx_file.exists():
                upload_items.append(docx_file)
            if self.synology_upload_json and json_file and json_file.exists():
                upload_items.append(json_file)

            if not upload_items:
                self.log("-> Keine Dateien für den Synology/WebDAV Upload ausgewählt oder vorhanden.")
                self._report_synology_confirmation([], 0)
                return uploads
            expected_count = len(upload_items)

            if hasattr(client.__class__, "upload_package_with_audit"):
                package_items = {}
                for p in upload_items:
                    role = (
                        "pdf" if p == pdf_file
                        else "docx" if p == docx_file
                        else "quality_report" if p == json_file
                        else p.suffix.lstrip(".")
                    )
                    package_items[role] = p
                self.log(
                    f"  Plane archivfestes Synology-Paket mit {len(package_items)} Datei(en) "
                    f"im Ordner '{target_path}'..."
                )
                package_audit = client.upload_package_with_audit(
                    package_items,
                    target_path,
                )
                if not isinstance(package_audit, list):
                    raise RuntimeError("Synology lieferte kein gültiges Paket-Audit.")
                uploads.extend(package_audit)
                for entry in package_audit:
                    self.log(
                        f"  ✔ Synology: {entry.get('remote_filename') or entry.get('filename')} "
                        f"{entry.get('action', 'uploaded')}"
                    )
                self._report_synology_confirmation(uploads, expected_count)
                return uploads

            for file_path in upload_items:
                p = Path(file_path)
                self.log(f"  Lade hoch: {p.name} nach Synology/WebDAV Ordner '{target_path}'...")
                try:
                    result = client.upload_file(p, target_path)
                    uploads.append(result)
                    self.log(f"  ✔ Erfolgreich synchronisiert: {p.name} ({result.get('remote_path')})")
                except Exception as upload_err:
                    self.log(f"  ⚠️ Fehler beim Synology/WebDAV Upload von '{p.name}': {upload_err}")
                    logger.exception(f"Synology/WebDAV Upload-Fehler für '{p.name}'")
                    uploads.append({
                        "provider": "synology_webdav",
                        "local_path": str(p),
                        "filename": p.name,
                        "folder_path": target_path,
                        "action": "failed",
                        "error": str(upload_err),
                    })
            self._report_synology_confirmation(uploads, expected_count)
        except Exception as e:
            self.log(f"⚠️ Synology/WebDAV Integration fehlgeschlagen: {e}")
            logger.exception("Synology/WebDAV Integration Fehler")
            uploads.append({
                "provider": "synology_webdav",
                "folder_path": target_path,
                "action": "failed",
                "error": str(e),
            })
            if expected_count:
                self._report_synology_confirmation(uploads, expected_count)
            else:
                self.report_workflow_status(
                    "synology",
                    "error",
                    f"Synology/NAS nicht bestätigt: {e}",
                    details={"expected_count": 0, "confirmed_count": 0, "error": str(e)},
                )
        return uploads

    # Beide Remote-Ziele durchlaufen denselben Ablauf: hochladen, Fehler in ein
    # Audit-Eintrag uebersetzen, Status aus Audit und Bestaetigungs-Summary
    # bilden, danach Manifest und Diagnostics schreiben.
    _SYNC_PROVIDERS = {
        "google_drive": {
            "workflow_step": "google_drive",
            "manifest_stage": "drive_upload",
            "audit_provider": "google_drive",
            "summary_attribute": "_last_google_drive_summary",
            "log_label": "Google Drive",
            "error_message": "Google Drive nicht bestätigt",
            "deferred_message": "Google-Drive-Upload wartet auf die Freigabe in der Review-Queue.",
            "disabled_message": "Google Drive ist für diesen Auftrag nicht aktiviert.",
        },
        "synology": {
            "workflow_step": "synology",
            "manifest_stage": "synology_upload",
            "audit_provider": "synology_webdav",
            "summary_attribute": "_last_synology_summary",
            "log_label": "Synology/WebDAV",
            "error_message": "Synology/NAS nicht bestätigt",
            "deferred_message": "Synology-/NAS-Upload wartet auf die Freigabe in der Review-Queue.",
            "disabled_message": "Synology/NAS ist für diesen Auftrag nicht aktiviert.",
        },
    }

    def _run_remote_sync(
        self,
        provider: str,
        *,
        enabled: bool,
        upload,
        manifest,
        diagnostics,
        is_deferred: bool,
        target_path: str,
        pdf_file,
        docx_file,
        json_file,
        is_docx: bool,
    ) -> tuple[list, str]:
        """Run one remote sync target and return its uploads and stage status."""
        spec = self._SYNC_PROVIDERS[provider]
        stage_name = spec["manifest_stage"]
        active = bool(enabled) and not is_deferred
        uploads: list = []

        if active:
            try:
                stage_start = time.perf_counter()
                uploads = upload(
                    pdf_file=pdf_file,
                    docx_file=docx_file,
                    json_file=json_file,
                    target_path=target_path,
                    is_docx_input=is_docx,
                ) or []
                if not isinstance(uploads, list):
                    uploads = []
                diagnostics.stage(
                    stage_name,
                    status="skipped" if not uploads else "ok",
                    start=stage_start,
                    uploads=uploads,
                )
            except Exception as upload_err:
                self.log(f"{spec['log_label']} Upload-Fehler: {upload_err}")
                diagnostics.warn(f"{spec['log_label']} Upload-Fehler", error=str(upload_err))
                self.report_workflow_status(
                    spec["workflow_step"],
                    "error",
                    f"{spec['error_message']}: {upload_err}",
                )
                uploads.append({
                    "provider": spec["audit_provider"],
                    "folder_path": target_path,
                    "action": "failed",
                    "error": str(upload_err),
                })
        else:
            waiting = bool(enabled) and is_deferred
            self.report_workflow_status(
                spec["workflow_step"],
                "warning" if waiting else "skipped",
                spec["deferred_message"] if waiting else spec["disabled_message"],
                details={"deferred": waiting},
            )

        status = self._upload_stage_status(uploads, enabled=active)
        # Die Bestaetigung aus dem Upload-Audit hat Vorrang vor der reinen
        # Eintragszaehlung.
        summary = getattr(self, spec["summary_attribute"], None)
        if isinstance(summary, dict):
            if summary.get("state") == "error":
                status = "failed"
            elif summary.get("state") == "success":
                status = "ok"

        diagnostics.stage(stage_name, status=status, uploads=uploads)
        if provider == "google_drive":
            manifest.record_drive_uploads(enabled=active, uploads=uploads)
        manifest.record_stage(
            stage_name,
            status,
            artifacts={"uploads": uploads},
            provenance={"target_path": target_path},
        )
        return uploads, status

    def _sync_published_review_package(self, context: dict, *, is_docx: bool) -> list[dict]:
        """Synchronize a locally committed review package before DB finalization."""
        artifacts = {
            role: Path(path)
            for role, path in (context.get("artifacts") or {}).items()
            if path and Path(path).is_file()
        }
        pdf_file = artifacts.get("pdf") or next(
            (path for path in artifacts.values() if path.suffix.lower() == ".pdf"),
            None,
        )
        docx_file = artifacts.get("reviewed_docx") or artifacts.get("docx") or next(
            (path for path in artifacts.values() if path.suffix.lower() == ".docx"),
            None,
        )
        json_file = artifacts.get("json") or artifacts.get("quality") or next(
            (
                path
                for path in artifacts.values()
                if path.suffix.lower() == ".json" and "quality" in path.name.casefold()
            ),
            None,
        )
        target_path = str(context.get("target_path") or "")
        uploads: list[dict] = []
        heartbeat = context.get("heartbeat")

        def renew_claim():
            if callable(heartbeat):
                heartbeat()

        if self.gdrive_enabled:
            renew_claim()
            try:
                uploads.extend(
                    self._stage_gdrive_upload(
                        pdf_file,
                        docx_file,
                        json_file,
                        target_path,
                        is_docx_input=is_docx,
                    )
                    or []
                )
            finally:
                renew_claim()
        if self.synology_enabled:
            renew_claim()
            try:
                uploads.extend(
                    self._stage_synology_upload(
                        pdf_file,
                        docx_file,
                        json_file,
                        target_path,
                        is_docx_input=is_docx,
                    )
                    or []
                )
            finally:
                renew_claim()
        return uploads

    def process_deferred_organizations(self):
        """Resolve current-session folder prompts through the durable queue.

        Every package has already been committed to ``final/_staging`` by
        :meth:`_stage_organize`.  A user cancellation therefore leaves a
        recoverable review item behind.  A confirmed decision is published by
        the same transactional service used by the GUI Review Queue; registry
        and learning state are updated only after the complete package move.
        """
        if not hasattr(self, "deferred_organizations") or not self.deferred_organizations:
            return

        self.log(f"\nVerarbeite {len(self.deferred_organizations)} zurückgestellte Ordner-Einsortierungen...")
        deferred_list = list(self.deferred_organizations)
        self.deferred_organizations.clear()

        from core.cloud.folder_registry import FolderRegistry
        from core.review_service import ReviewQueueService, ReviewResolutionError

        store = LocalStore(self.config)
        service = ReviewQueueService(self.config)
        for item in deferred_list:
            final_name = item["final_name"]
            proposed_path = item["proposed_path"]
            review_item_id = item.get("review_item_id")
            preview_pdf_path = item.get("preview_pdf_path")
            is_docx = bool(item.get("is_docx"))

            self.log(f"\n--- Einsortierung für '{final_name}' ---")
            target_path = proposed_path
            try:
                registry = FolderRegistry(self.config.base_dir)
                known_paths = registry.get_known_paths()
                if target_path not in known_paths:
                    if not self.prompt_new_folder_callback:
                        self.log("  Keine Benutzerentscheidung verfügbar; Paket bleibt in der Review-Queue.")
                        continue
                    self.log(f"Warte auf Benutzerentscheidung für Ordner: '{target_path}'...")
                    chosen_path = self._call_compatible_callback(
                        self.prompt_new_folder_callback,
                        target_path,
                        preview_pdf_path,
                    )
                    if not chosen_path:
                        self.log("  Entscheidung zurückgestellt; Paket bleibt vollständig im Staging.")
                        continue
                    target_path = str(chosen_path)

                result = service.resolve(
                    int(review_item_id),
                    target_path,
                    quality_confirmed=False,
                    post_publish_callback=(
                        (lambda context: self._sync_published_review_package(context, is_docx=is_docx))
                        if (self.gdrive_enabled or self.synology_enabled)
                        else None
                    ),
                    review_note="Zielpfad im Ordnerdialog bestätigt.",
                )
                target_path = result["target_path"]
                artifacts = {
                    role: Path(path)
                    for role, path in result.get("artifacts", {}).items()
                    if path and Path(path).is_file()
                }
                self._last_organize_audit = list(result.get("audit") or [])
                self.log(f"-> Vollständiges Paket einsortiert in: final/{target_path}")

                # Review evidence is retained as one coherent package.  Removing
                # a cloud-only DOCX/quality sidecar here would make the resolved
                # queue row, document index and manifest claim files that no
                # longer exist.  Storage preferences continue to control which
                # derivatives are created before review; once used as evidence,
                # they remain part of the archival package.
            except ReviewResolutionError as exc:
                self.log(f"  Review-Ablage fehlgeschlagen; Paket bleibt wiederaufnehmbar: {exc}")
                logger.exception("Fehler bei verzögerter Paketablage")
            except Exception as exc:
                self.log(f"  Nachbearbeitung nach Review fehlgeschlagen: {exc}")
                logger.exception("Fehler bei verzögerter Ablage/Synchronisation")

    # ------------------------------------------------------------------ #
    #  Haupt-Einstiegspunkt                                                #
    # ------------------------------------------------------------------ #

    def _phase_extract_document(
        self,
        original_path: Path,
        work_dir: Path,
        *,
        suffix: str,
        is_docx: bool,
        filename: str,
        manifest,
        diagnostics,
    ) -> ExtractionResult:
        """Stufen 1 bis 5: Text gewinnen, aufbereiten und pruefen.

        Deckt die drei Wege ab: Office-Direktimport, reduzierte Analyse fuer
        sehr grosse PDFs und die vollstaendige Seitenverarbeitung. Nur fuenf
        Werte verlassen diese Phase; sie stehen im ExtractionResult.
        """
        if is_docx:
            self.report_workflow_status(
                "ocr",
                "running",
                f"Text wird direkt aus dem Office-Dokument ({suffix.upper()[1:]}) übernommen.",
            )
            self.log(f"Direkter Bypass-Modus für Office-Dokument ({suffix.upper()[1:]}) aktiv. Überspringe OCR und Seitenextraktion.")
            stage_start = time.perf_counter()
            ocr_text = self._extract_office_text(original_path, suffix)


            fused_text = ocr_text
            fused_pages = {1: fused_text}
            image_paths = []
            quality_report = {}
            page_layout_blocks = {}
            source_pdf_for_export = original_path
            manifest.record_stage("office_extract", "ok", artifacts={"source": original_path})
            diagnostics.stage("office_extract", start=stage_start, suffix=suffix, text_chars=len(ocr_text or ""))
            diagnostics.record_text_sources(office_text=ocr_text)
            self.report_workflow_status(
                "ocr",
                "success",
                f"Office-Text erfolgreich übernommen ({len(ocr_text or '')} Zeichen); bildbasierte OCR war nicht nötig.",
                details={"mode": "office_text_extract", "text_chars": len(ocr_text or "")},
            )
            self.report_workflow_status(
                "quality",
                "skipped",
                "Die mehrstufige OCR-Qualitätsprüfung ist für den Office-Direktimport nicht anwendbar.",
            )
        else:
            # ── Stage 1: Vorbereitung & OCR ──────────────────────────────
            self.report_workflow_status(
                "ocr",
                "running",
                "OCR, Seitenanalyse und Textaufbereitung werden ausgeführt.",
            )
            self.report_progress(0.10)
            stage_start = time.perf_counter()
            work_pdf = self._stage_prepare(original_path, work_dir)
            manifest.record_stage("prepare", "ok", artifacts={"work_pdf": work_pdf})
            diagnostics.stage("prepare", start=stage_start, work_pdf=work_pdf)
            self.report_progress(0.15)
            stage_start = time.perf_counter()
            ocr_pdf, ocr_text = self._stage_ocrmypdf(work_pdf, work_dir)
            manifest.record_stage(
                "ocrmypdf",
                "ok",
                artifacts={"ocr_pdf": ocr_pdf},
                warnings=list((self._last_ocr_preflight or {}).get("warnings", [])),
                provenance={
                    "text_chars": len(ocr_text or ""),
                    "preflight": self._last_ocr_preflight,
                },
            )
            diagnostics.stage(
                "ocrmypdf",
                start=stage_start,
                ocr_pdf=ocr_pdf,
                text_chars=len(ocr_text or ""),
                preflight=self._last_ocr_preflight,
            )
            diagnostics.record_text_sources(ocr_sidecar=ocr_text)
            self.report_progress(0.30)

            # Seitenanzahl ermitteln
            total_pages = 1
            try:
                import fitz
                if fitz:
                    with fitz.open(ocr_pdf) as doc:
                        total_pages = len(doc)
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
                page_layout_blocks = {}
                quality_report = self._build_reduced_analysis_report(total_pages)
                self._merge_ocr_preflight_quality(quality_report)
                source_pdf_for_export = ocr_pdf
                manifest.record_quality(quality_report)
                manifest.record_stage("reduced_analysis", "degraded", warnings=quality_report["warnings"], provenance={"total_pages": total_pages})
                diagnostics.stage("reduced_analysis", status="degraded", total_pages=total_pages, page_limit=self.config.large_pdf_page_limit)
                diagnostics.warn("Reduzierte Analyse aktiv.", total_pages=total_pages, page_limit=self.config.large_pdf_page_limit)
                self.report_workflow_status(
                    "ocr",
                    "warning",
                    f"OCR abgeschlossen; Detailanalyse wurde wegen {total_pages} Seiten reduziert.",
                    details={"total_pages": total_pages, "reduced_analysis": True},
                )
                self.report_workflow_status(
                    "quality",
                    "warning",
                    "Detaillierte Qualitätsprüfung wurde im Schnellmodus reduziert; manuelle Prüfung erforderlich.",
                    details={"quality_score": quality_report.get("quality_score"), "total_pages": total_pages},
                )
            else:
                # Detaillierte Analyse
                # ── Stage 2: Docling & Seitenextraktion ───────────────────────
                stage_start = time.perf_counter()
                docling_text, page_mds = self._stage_docling(ocr_pdf, work_pdf)
                manifest.record_stage("docling", "ok", provenance={"text_chars": len(docling_text or ""), "pages": len(page_mds or {})})
                diagnostics.stage("docling", start=stage_start, text_chars=len(docling_text or ""), pages=len(page_mds or {}))
                diagnostics.record_text_sources(docling_text=docling_text, docling_pages=page_mds)
                self.report_progress(0.35)
                stage_start = time.perf_counter()
                image_paths, page_ocr_texts = self._stage_extract_pages(ocr_pdf, work_dir)
                page_layout_blocks = extract_ordered_text_blocks_per_page(ocr_pdf)
                manifest.record_stage(
                    "page_extract",
                    "ok",
                    artifacts={"images": image_paths},
                    provenance={
                        "pages": len(image_paths or []),
                        "layout_blocks": sum(len(v or []) for v in (page_layout_blocks or {}).values()),
                    },
                )
                layout_summary = self._summarize_layout_blocks(page_layout_blocks)
                diagnostics.stage(
                    "page_extract",
                    start=stage_start,
                    images=len(image_paths or []),
                    ocr_pages=len(page_ocr_texts or {}),
                    layout_blocks=sum(len(v or []) for v in (page_layout_blocks or {}).values()),
                )
                diagnostics.record_text_sources(page_ocr=page_ocr_texts)
                diagnostics.record_layout(layout_summary)
                self.report_progress(0.40)

                # ── Stage 2b & 3: GLM-OCR & Vision-Review ────────────────────
                is_tabular = self._detect_tabular(filename, page_mds)
                stage_start = time.perf_counter()
                glm_texts = self._stage_glm_ocr(image_paths, total_pages)
                manifest.record_stage("glm_ocr", "skipped" if not any((glm_texts or {}).values()) else "ok", provenance={"pages": len(glm_texts or {})})
                diagnostics.stage("glm_ocr", status="skipped" if not any((glm_texts or {}).values()) else "ok", start=stage_start, pages=len(glm_texts or {}))
                diagnostics.record_text_sources(glm_pages=glm_texts)
                stage_start = time.perf_counter()
                vision_mds, page_descriptions = self._stage_vision_review(
                    image_paths, page_mds, page_ocr_texts, total_pages
                )
                manifest.record_stage("vision", "skipped" if not any((vision_mds or {}).values()) else "ok", provenance={"pages": len(vision_mds or {}), "descriptions": len(page_descriptions or {})})
                diagnostics.stage(
                    "vision",
                    status="skipped" if not any((vision_mds or {}).values()) else "ok",
                    start=stage_start,
                    pages=len(vision_mds or {}),
                    descriptions=len(page_descriptions or {}),
                )
                diagnostics.record_text_sources(vision_pages=vision_mds, image_descriptions=page_descriptions)

                # ── Stage 4: Fusion ──────────────────────────────────────────
                stage_start = time.perf_counter()
                fused_pages = self._stage_fusion(
                    image_paths, page_ocr_texts, vision_mds, page_mds, glm_texts, is_tabular, total_pages
                )
                fusion_status = "ok" if any((v or "").strip() for v in (fused_pages or {}).values()) else "degraded"
                manifest.record_stage("fusion", fusion_status, provenance={"pages": len(fused_pages or {}), "is_tabular": is_tabular})
                diagnostics.stage("fusion", status=fusion_status, start=stage_start, pages=len(fused_pages or {}), is_tabular=is_tabular)
                diagnostics.record_text_sources(fused_pages=fused_pages)
                self.report_workflow_status(
                    "ocr",
                    "success" if fusion_status == "ok" else "warning",
                    (
                        "OCR und seitenweise Textaufbereitung wurden abgeschlossen."
                        if fusion_status == "ok"
                        else "OCR wurde abgeschlossen; für die Textaufbereitung war ein Fallback nötig."
                    ),
                    details={"pages": total_pages, "fusion_status": fusion_status},
                )

                # ── Stage 5: Qualitätskontrolle ──────────────────────────────
                self.report_workflow_status(
                    "quality",
                    "running",
                    "OCR-, Layout- und Fusionsquellen werden miteinander verglichen.",
                )
                self.report_progress(0.82)
                vision_combined = "\n\n".join(vision_mds.values())

                initial_fused_text = "\n\n".join(fused_pages.values()) if fused_pages else ocr_text
                stage_start = time.perf_counter()
                fused_text_corrected, quality_report = self._stage_quality(ocr_text, docling_text, vision_combined, initial_fused_text)
                if isinstance(quality_report, dict):
                    quality_report["layout_packets"] = layout_summary
                    quality_report["visual_descriptions"] = {
                        str(page): description
                        for page, description in sorted((page_descriptions or {}).items())
                    }
                source_pdf_for_export = ocr_pdf if ocr_pdf and Path(ocr_pdf).exists() else work_pdf
                quality_status = (quality_report or {}).get("quality_status", "review")
                manifest.record_quality(quality_report)
                manifest.record_stage(
                    "quality",
                    quality_status,
                    warnings=(quality_report or {}).get("warnings", []),
                    provenance={
                        "corrected": False,
                        "requires_review": bool((quality_report or {}).get("requires_review")),
                    },
                )
                diagnostics.stage(
                    "quality",
                    status=quality_status,
                    start=stage_start,
                    corrected=False,
                    requires_review=bool((quality_report or {}).get("requires_review")),
                    warnings=(quality_report or {}).get("warnings", []),
                )
                diagnostics.record_text_sources(fused_document=fused_text_corrected)
                quality_workflow_state = self._quality_workflow_state(quality_report)
                self.report_workflow_status(
                    "quality",
                    quality_workflow_state,
                    (
                        "Qualitätsprüfung ohne relevante Auffälligkeiten abgeschlossen."
                        if quality_workflow_state == "success"
                        else "Qualitätsprüfung abgeschlossen; das Dokument muss geprüft werden."
                        if quality_workflow_state == "warning"
                        else "Kritische Qualitätsabweichung erkannt; Veröffentlichung erfordert eine Prüfung."
                    ),
                    details={
                        "quality_status": quality_status,
                        "quality_score": (quality_report or {}).get("quality_score"),
                        "warnings": list((quality_report or {}).get("warnings") or []),
                    },
                )

                # Visual descriptions are useful enrichment, but they are
                # not a transcription and must never be injected into the
                # OCR text or hidden PDF layer.  Keep them in the quality
                # sidecar above and preserve the page transcription here.
                fused_text = fused_text_corrected or initial_fused_text or ocr_text

        return ExtractionResult(
            ocr_text=ocr_text,
            fused_text=fused_text,
            fused_pages=fused_pages,
            image_paths=image_paths,
            quality_report=quality_report,
            source_pdf_for_export=source_pdf_for_export,
        )

    def _phase_finalize_job(
        self,
        *,
        job_id: str,
        filename: str,
        final_name: str,
        target_path: str,
        metadata: dict,
        quality_report: dict,
        exported_paths: dict,
        is_deferred: bool,
        drive_status: str,
        synology_status: str,
        source_sha256: str,
        manifest,
        diagnostics,
        local_store,
        job_history,
    ) -> str:
        """Auditnachweise sichern, Job abschliessen und den Endstatus liefern.

        Umfasst Manifest- und Diagnosekopien, das Verschieben der Nachweise
        zu einem zurueckgestellten Paket, den Dokumentindex sowie Historie
        und persistenten Jobstatus. Fehler hier duerfen das fertige
        Ausgabepaket nicht entwerten und werden deshalb nur protokolliert.
        """
        final_job_status = "completed"
        try:
            quality_state = str((quality_report or {}).get("quality_status") or "ok")
            organize_failed = any(
                entry.get("action") == "move_failed"
                for entry in (getattr(self, "_last_organize_audit", []) or [])
            )
            if is_deferred:
                final_job_status = "review_required"
            elif drive_status == "failed" or synology_status == "failed":
                final_job_status = "sync_failed"
            elif quality_state in {"review", "critical"} or organize_failed:
                final_job_status = "completed_with_warnings"
            else:
                final_job_status = "completed"

            evidence_name = f"{final_name}_{job_id[:12]}"
            manifest_path = self.config.final_dir / "begleitdateien" / f"{evidence_name}_job_manifest.json"
            debug_report_path = self.config.final_dir / "begleitdateien" / f"{evidence_name}_debug_report.json"
            diagnostics.event("job_finished", status=final_job_status, manifest_path=manifest_path)
            written_debug_report = diagnostics.write_copy(debug_report_path)
            manifest.record_stage(
                "diagnostics",
                "ok" if written_debug_report else "skipped",
                artifacts={"debug_report": written_debug_report},
            )
            manifest.finalize(final_job_status)
            manifest.write_copy(manifest_path)
            audit_artifacts = {
                "job_manifest": manifest_path,
                "debug_report": written_debug_report,
            }
            if is_deferred:
                # The final audit evidence belongs to the staged package as
                # well.  Otherwise a restart-safe review could move the
                # document but leave its manifest and diagnostics orphaned
                # in the publication directory.
                staging_member = next(
                    (
                        Path(value)
                        for value in (exported_paths or {}).values()
                        if value and Path(value).is_file()
                    ),
                    None,
                )
                if staging_member is None:
                    raise FileNotFoundError("Kein Staging-Artefakt für Auditnachweise gefunden.")
                from core.cloud.organizer import DocumentOrganizer

                evidence_sources = {
                    role: Path(path)
                    for role, path in audit_artifacts.items()
                    if path and Path(path).is_file()
                }
                active_review = local_store.get_review_by_job_id(job_id)
                if not active_review:
                    raise RuntimeError(
                        "Der Review-Eintrag für die Auditnachweise ist nicht mehr verfügbar."
                    )
                evidence_payload = dict(active_review.get("payload") or {})
                evidence_payload["evidence_move_intent"] = self._build_package_move_intent(
                    evidence_sources,
                    target_dir=staging_member.parent,
                    target_label=f"_staging/{job_id}",
                )
                # Persist source hashes and the intended destination before
                # moving manifest/debug evidence.  Recovery can reconcile
                # either side of the filesystem/SQLite commit window.
                local_store.update_review_item(
                    int(active_review["id"]),
                    status="staged",
                    artifacts={
                        **dict(active_review.get("artifacts") or {}),
                        **{role: str(path) for role, path in evidence_sources.items()},
                    },
                    payload=evidence_payload,
                    metadata=metadata,
                    quality=quality_report or {},
                )
                evidence_moved = DocumentOrganizer(self.config.final_dir).move_artifacts_to_directory(
                    evidence_sources,
                    staging_member.parent,
                    target_label=f"_staging/{job_id}",
                    package_id=f"{job_id}-evidence",
                )
                audit_artifacts = {
                    role: path
                    for role, path in zip(evidence_sources, evidence_moved)
                }
                # Manifest and diagnostics were finalized before their
                # hashes entered the move journal.  Do not rewrite them in
                # the destination here: a crash between such a rewrite and
                # the SQLite commit would invalidate the only recovery
                # hashes.  Review finalization updates the manifest again
                # after the complete package reaches its definitive path.

                review_artifacts = {
                    **{
                        key: str(value)
                        for key, value in (exported_paths or {}).items()
                        if value and Path(value).is_file()
                    },
                    **{
                        key: str(value)
                        for key, value in audit_artifacts.items()
                        if value and Path(value).is_file()
                    },
                }
                if active_review:
                    evidence_payload["evidence_move_intent"] = {
                        **evidence_payload["evidence_move_intent"],
                        "phase": "committed",
                        "destinations": {
                            role: str(path) for role, path in audit_artifacts.items()
                        },
                        "committed_at_epoch": time.time(),
                    }
                    local_store.update_review_item(
                        int(active_review["id"]),
                        status="staged",
                        artifacts=review_artifacts,
                        metadata=metadata,
                        quality=quality_report or {},
                        payload=evidence_payload,
                    )
            if not is_deferred:
                local_store.index_document(
                    source_sha256=source_sha256,
                    source_name=filename,
                    final_name=final_name,
                    target_path=target_path,
                    outputs=exported_paths,
                    metadata=metadata,
                )
            job_history.finish(
                job_id,
                final_job_status,
                source_name=filename,
                final_name=final_name,
                target_path=target_path,
                metadata=metadata,
            )
            local_store.update_job(
                job_id,
                final_job_status,
                final_name=final_name,
                target_path=target_path,
                metadata=metadata,
                artifacts={
                    **{
                        key: str(value) if value else ""
                        for key, value in (exported_paths or {}).items()
                    },
                    **{
                        key: str(value) if value else ""
                        for key, value in audit_artifacts.items()
                    },
                },
                quality=quality_report or {},
            )
        except Exception as history_err:
            logger.warning(f"Job-Historie konnte nicht geschrieben werden: {history_err}")
        return final_job_status

    def process_file(self, file_path: Path):
        self._enforce_privacy_mode()
        # Ein frischer Kontext pro Dokument. Frueher stand hier eine Liste
        # einzelner Zuweisungen, in der vier Felder fehlten; ein Office-Dokument
        # erbte dadurch die OCR-Preflight-Warnung des vorherigen PDFs.
        #
        # manifest_required: Reviews aus der Live-Pipeline muessen ihr dauerhaftes
        # Job-Manifest behalten, bevor der ReviewQueueService sie veroeffentlichen
        # darf. Isolierte Legacy-/Import-Workflows lassen den Marker weg.
        self._begin_job(manifest_required=True)
        filename = file_path.name
        metadata = {}
        final_name = ""
        target_path = ""
        manifest = None
        diagnostics = None
        self._current_source_name = filename
        source_input_dir = self.config.source_consume_dir_for(file_path) if hasattr(self.config, "source_consume_dir_for") else None
        if source_input_dir:
            source_input_profile = "consume" if source_input_dir == self.config.consume_dir else source_input_dir.name
        else:
            source_input_profile = "manual"
        source_sha256 = sha256_file(file_path)
        local_store = LocalStore(self.config)
        duplicate_matches = local_store.find_duplicates(source_sha256)
        job_history = JobHistory(self.config)
        job_id = job_history.start(file_path)
        self._current_job_id = job_id
        self.report_workflow_status(
            "job",
            "running",
            f"Statusanzeige für {filename} wurde zurückgesetzt.",
            details={"reset": True},
        )
        self.report_workflow_status(
            "input",
            "running",
            "Eingang wird übernommen und das unveränderte Original gesichert.",
        )
        local_store.update_job(
            job_id,
            "started",
            payload={
                "source_input_dir": str(source_input_dir) if source_input_dir else "",
                "source_input_profile": source_input_profile,
            },
        )
        # Create durable job evidence before moving the source.  A crash
        # between ingestion and work-manifest creation must never leave an
        # unexplained file in ``original``.
        work_dir = self.config.work_dir / f"job_{job_id}"
        work_dir.mkdir(parents=True, exist_ok=True)
        manifest = JobManifest.create(job_id=job_id, source_path=file_path, manifest_dir=work_dir)
        manifest.record_source_context(input_dir=source_input_dir, input_profile=source_input_profile)
        diagnostics = DiagnosticsRecorder(
            job_id=job_id,
            source_path=file_path,
            enabled=self.debug_artifacts_enabled,
        )
        self.log(f"\n{'─' * 50}")
        self.log(f"Starte Verarbeitung: {filename}")
        if duplicate_matches:
            match = duplicate_matches[0]
            self.log(
                "⚠️ Mögliches Duplikat erkannt: "
                f"{match.get('final_name') or match.get('source_name')} in {match.get('target_path') or 'unbekannt'}"
            )
            local_store.record_event(
                job_id,
                "duplicate_detected",
                status="warning",
                payload={"source_sha256": source_sha256, "matches": duplicate_matches},
            )
        self.report_progress(0.05)

        # Datei ins original-Verzeichnis sichern
        original_path = unique_path_for(self.config.original_dir, file_path)
        try:
            shutil.move(str(file_path), str(original_path))
        except Exception as e:
            self.log(f"Konnte Datei nicht verschieben: {e}")
            logger.exception(f"Datei-Move fehlgeschlagen: {filename}")
            self.report_workflow_status(
                "input",
                "error",
                f"Original konnte nicht gesichert werden: {e}",
            )
            self.report_workflow_status(
                "complete",
                "error",
                "Verarbeitung wurde beendet, weil das Original nicht sicher archiviert werden konnte.",
            )
            manifest.record_stage("original_archive", "failed", warnings=[str(e)])
            manifest.finalize("failed", error=str(e))
            evidence_dir = self.config.error_dir / job_id
            manifest.write_copy(evidence_dir / "job_manifest.json")
            diagnostics.warn("Original konnte nicht archiviert werden", error=str(e))
            diagnostics.write_copy(evidence_dir / "debug_report.json")
            job_history.finish(job_id, "failed", source_name=filename, error=str(e))
            try:
                local_store.update_job(job_id, "failed", error=str(e))
            except Exception as store_err:
                logger.warning(f"Persistenter Jobstatus konnte nicht geschrieben werden: {store_err}")
            self._current_job_id = ""
            self._current_source_name = ""
            self._active_workflow_step = ""
            try:
                remove_directory_tree(work_dir)
            except OSError:
                pass
            return {
                "status": "failed",
                "job_id": job_id,
                "source_name": filename,
                "error": str(e),
            }

        self.log(f"Datei nach '{self.config.original_dir.name}' verschoben.")
        self._current_original_path = original_path
        self.report_workflow_status(
            "input",
            "success",
            f"Unverändertes Original gesichert: {original_path.name}",
            details={"original_path": str(original_path), "source_sha256": source_sha256},
        )
        manifest.record_original_archive(original_path)
        local_store.update_job(
            job_id,
            "processing",
            source_path=str(original_path),
            artifacts={"original": str(original_path)},
        )

        if self.on_processing_start_callback:
            try:
                self.on_processing_start_callback(original_path)
            except Exception as e:
                logger.exception("Fehler in on_processing_start_callback")

        diagnostics.configure(
            output_format=self.output_format,
            docx_mode=self.docx_mode,
            organize_enabled=self.organize_enabled,
            privacy_mode=self.privacy_mode,
            gdrive_enabled=self.gdrive_enabled,
            gdrive_upload_pdf=self.gdrive_upload_pdf,
            gdrive_upload_docx=self.gdrive_upload_docx,
            gdrive_upload_json=self.gdrive_upload_json,
            synology_enabled=self.synology_enabled,
            synology_base_url=self.synology_base_url,
            synology_root_path=self.synology_root_path,
            synology_upload_pdf=self.synology_upload_pdf,
            synology_upload_docx=self.synology_upload_docx,
            synology_upload_json=self.synology_upload_json,
            save_docx_enabled=self.save_docx_enabled,
            save_json_enabled=self.save_json_enabled,
            large_pdf_reduced=self.large_pdf_reduced,
            source_input_dir=source_input_dir,
            source_input_profile=source_input_profile,
            configured_input_dirs=getattr(self.config, "consume_dirs", [self.config.consume_dir]),
            models={
                "glm_ocr": getattr(self.llm, "glm_ocr_model", ""),
                "vision": getattr(self.llm, "vision_model", ""),
                "fusion": getattr(self.llm, "fusion_model", ""),
                "analysis": getattr(self.llm, "analysis_model", ""),
            },
        )
        diagnostics.event(
            "job_started",
            filename=filename,
            source_sha256=source_sha256,
            source_input_dir=source_input_dir,
            source_input_profile=source_input_profile,
        )
        manifest.record_stage("original_archive", "ok", artifacts={"original_path": original_path})
        if duplicate_matches:
            diagnostics.warn("Mögliches Duplikat erkannt.", matches=duplicate_matches)
            manifest.record_stage(
                "duplicate_check",
                "warning",
                warnings=["Mögliches Duplikat erkannt."],
                provenance={"source_sha256": source_sha256, "matches": duplicate_matches},
            )

        try:
            suffix = original_path.suffix.lower()
            is_docx = suffix in SUPPORTED_OFFICE_SUFFIXES

            extraction = self._phase_extract_document(
                original_path,
                work_dir,
                suffix=suffix,
                is_docx=is_docx,
                filename=filename,
                manifest=manifest,
                diagnostics=diagnostics,
            )
            fused_text = extraction.fused_text
            fused_pages = extraction.fused_pages
            image_paths = extraction.image_paths
            quality_report = extraction.quality_report
            source_pdf_for_export = extraction.source_pdf_for_export

            # ── Stage 6: Metadaten-Analyse ────────────────────────────────
            self.report_workflow_status(
                "metadata",
                "running",
                "Archivmetadaten, Dokumentart und Tags werden extrahiert und belegt.",
            )
            self.report_progress(0.88)
            stage_start = time.perf_counter()
            self._analysis_source_pages = (
                fused_pages if isinstance(fused_pages, dict) and fused_pages else None
            )
            metadata, final_name = self._stage_analysis(fused_text)
            manifest.record_stage("analysis", "ok" if metadata else "degraded", provenance={"final_name": final_name})
            diagnostics.stage(
                "analysis",
                status="ok" if metadata else "degraded",
                start=stage_start,
                final_name=final_name,
                metadata=metadata,
            )
            self.report_progress(0.90)

            # ── Review Before Save ────────────────────────────────────────
            if self.review_before_save and self.prompt_review_callback:
                self.log("Warte auf Benutzerüberprüfung...")
                review_start = time.perf_counter()
                pre_target_path = "Sonstiges"
                if self.organize_enabled:
                    try:
                        from core.cloud.folder_registry import FolderRegistry
                        registry = FolderRegistry(self.config.base_dir)
                        known_paths = registry.get_known_paths()
                        res = self.llm.run_classification(
                            fused_text,
                            metadata,
                            known_paths,
                            registry.get_persons(),
                            registry.get_path_contexts(),
                        )
                        pre_target_path = res.get("recommended_path", "Sonstiges")
                    except Exception as e:
                        logger.exception("Pre-classification failed")

                review_res = self._call_compatible_callback(
                    self.prompt_review_callback,
                    source_pdf_for_export,
                    fused_text,
                    metadata,
                    pre_target_path,
                    quality_report,
                    original_path,
                )
                review_deferred = review_res is None
                if review_deferred:
                    # Closing the review is a safe deferral, never a failed
                    # document job.  The normal quality gate below will place
                    # the complete exported package in the durable queue and
                    # keep every remote upload blocked until confirmation.
                    self.log("Review wurde zur späteren Prüfung zurückgestellt.")
                    quality_report = self._mark_review_deferred(quality_report)
                    review_res = (fused_text, metadata, "", "")
                    self.report_workflow_status(
                        "quality",
                        "warning",
                        "Review zurückgestellt; das Paket wird sicher in die Review-Queue übernommen.",
                        details={"review_reason": "manual_review_deferred"},
                    )
                
                previous_review_text = fused_text
                updated_fused_text, updated_metadata, custom_final_name, chosen_target_path = review_res
                fused_text = updated_fused_text
                metadata = normalize_metadata(
                    updated_metadata,
                    source_text=fused_text,
                    source_pages=fused_pages if isinstance(fused_pages, dict) and fused_pages else None,
                )
                self._chosen_target_path = chosen_target_path if not review_deferred else None
                self._manual_review_completed = not review_deferred
                diagnostics.stage(
                    "manual_review",
                    status="deferred" if review_deferred else "ok",
                    start=review_start,
                    chosen_target_path=chosen_target_path,
                    custom_final_name=custom_final_name,
                    text_changed=updated_fused_text != previous_review_text,
                )
                if custom_final_name:
                    final_name = self._sanitize_final_name(custom_final_name)
                else:
                    final_name = self._final_name_from_metadata(metadata)
                
                if len(fused_pages or {}) <= 1:
                    fused_pages = {1: fused_text}
                elif updated_fused_text != previous_review_text:
                    self.log("Manuelle Review hat Dokumenttext geändert. PDF-Textlayer bleibt seitenweise erhalten; TXT/DOCX nutzen den geprüften Gesamttext.")

                manifest.record_review({
                    "status": "deferred" if review_deferred else "confirmed",
                    "chosen_target_path": chosen_target_path,
                    "custom_final_name": custom_final_name,
                    "text_changed": updated_fused_text != previous_review_text,
                    "metadata": metadata,
                })

            metadata_evidence = assess_metadata_evidence(
                metadata,
                fused_text,
                manually_confirmed=bool(getattr(self, "_manual_review_completed", False)),
            )
            if not isinstance(quality_report, dict):
                quality_report = {}
            self._merge_metadata_evidence_quality(quality_report, metadata_evidence)
            self._merge_filename_quality(quality_report)
            manifest.record_quality(quality_report)
            diagnostics.stage(
                "metadata_evidence",
                status=metadata_evidence.get("status", "review"),
                requires_review=bool(metadata_evidence.get("requires_review")),
                unverified_fields=metadata_evidence.get("unverified_fields", []),
            )
            analysis_model = str(getattr(self.llm, "analysis_model", "") or "")
            if not analysis_model or analysis_model == "Keins":
                metadata_workflow_state = "skipped"
                metadata_message = "Kein Analysemodell aktiv; Metadaten und Tags bleiben unbekannt oder manuell gepflegt."
            elif metadata_evidence.get("requires_review"):
                metadata_workflow_state = "warning"
                metadata_message = "Metadaten und Tags wurden extrahiert; einzelne Angaben benötigen eine Prüfung."
            else:
                metadata_workflow_state = "success"
                metadata_message = "Metadaten und Tags wurden extrahiert und gegen den Dokumenttext geprüft."
            self.report_workflow_status(
                "metadata",
                metadata_workflow_state,
                metadata_message,
                details={
                    "title": metadata.get("title", ""),
                    "document_type": metadata.get("document_type", ""),
                    "tags": list(metadata.get("tags") or []),
                    "evidence_status": metadata_evidence.get("status", ""),
                    "unverified_fields": list(metadata_evidence.get("unverified_fields") or []),
                },
            )

            # Persist effective post-review metadata.  The earlier
            # pre-classification values are diagnostics only and must not
            # become the authoritative archive record.
            manifest.record_metadata(metadata)

            self.report_progress(0.92)

            # ── Stage 7: Export ───────────────────────────────────────────
            self.report_workflow_status(
                "export",
                "running",
                "Das vollständige Ausgabepaket wird transaktionssicher erzeugt.",
            )
            if isinstance(quality_report, dict):
                self._merge_ocr_preflight_quality(quality_report)
                quality_report["runtime_audit"] = build_runtime_audit(
                    self.llm,
                    output_format=self.output_format,
                    docx_mode=self.docx_mode,
                    large_pdf_reduced=self.large_pdf_reduced,
                    ocr_options=self._last_ocr_preflight,
                )
                quality_report["diagnostics"] = {
                    "enabled": self.debug_artifacts_enabled,
                    "schema": "unified_ocr_diagnostics_v1",
                    "note": "Vollständiger lokaler Diagnosebericht wird als separate *_debug_report.json gespeichert.",
                }
            stage_start = time.perf_counter()
            exported_paths = self._stage_export(source_pdf_for_export, fused_pages, fused_text, final_name, metadata, image_paths, quality_report, is_docx=is_docx)
            # PDF finalization rewrites XMP/document info.  The exporter runs
            # the postflight after that last byte-level change and appends the
            # result to the shared quality object.  Persist this authoritative
            # state instead of leaving the manifest at its pre-export view.
            if isinstance(quality_report, dict) and quality_report.get("pdf_postflight"):
                manifest.record_quality(quality_report)
            effective_export_name = str(getattr(self, "_last_export_final_name", "") or final_name)
            if effective_export_name != final_name:
                self.log(
                    f"Namenskonflikt im Veröffentlichungsbereich: Paketname '{effective_export_name}' wird verwendet."
                )
                final_name = effective_export_name
            if not isinstance(exported_paths, dict):
                begleit_dir = self.config.final_dir / "begleitdateien"
                exported_paths = {
                    "pdf": self.config.final_dir / f"{final_name}.pdf",
                    "txt": self.config.final_dir / f"{final_name}.txt",
                    "docx": (self.config.final_dir / f"{final_name}.docx") if is_docx else (begleit_dir / f"{final_name}.docx"),
                    "json": begleit_dir / f"{final_name}_quality_report.json",
                }
            manifest.record_outputs(exported_paths)
            manifest.record_stage("export", "ok", artifacts=exported_paths)
            diagnostics.stage("export", start=stage_start, outputs=exported_paths)
            diagnostics.record_outputs(exported_paths)
            self.report_workflow_status(
                "export",
                "success",
                "PDF/TXT/DOCX und Begleitdateien wurden entsprechend der Auswahl sicher erstellt.",
                details={
                    "outputs": {
                        key: str(value) if value else ""
                        for key, value in (exported_paths or {}).items()
                    }
                },
            )

            # ── Stage 8: Sortieren / Organize ─────────────────────────────
            moved_files = []
            target_path = ""
            if self.organize_enabled:
                self.report_workflow_status(
                    "archive",
                    "running",
                    "Zielordner wird ermittelt und das Dokumentpaket gemeinsam abgelegt.",
                )
                self.report_progress(0.96)
                stage_start = time.perf_counter()
                sorting_preview_path = self._resolve_exported_path(exported_paths, "pdf") or source_pdf_for_export
                moved_files, target_path = self._stage_organize(
                    fused_text,
                    metadata,
                    final_name,
                    is_docx=is_docx,
                    preview_pdf_path=sorting_preview_path,
                    artifact_paths=exported_paths,
                    quality_report=quality_report,
                )
                organize_audit = list(getattr(self, "_last_organize_audit", []) or [])
                organization_deferred = bool(getattr(self, "_current_organization_deferred", False))
                manifest.record_stage(
                    "organize",
                    "deferred" if organization_deferred else "ok",
                    artifacts={"moved_files": moved_files},
                    provenance={"target_path": target_path, "audit": organize_audit},
                )
                diagnostics.stage(
                    "organize",
                    status="deferred" if organization_deferred else "ok",
                    start=stage_start,
                    target_path=target_path,
                    moved_files=moved_files,
                    audit=organize_audit,
                )
                self.report_workflow_status(
                    "archive",
                    "warning" if organization_deferred else "success",
                    (
                        "Ablage wartet in der Review-Queue auf eine Bestätigung."
                        if organization_deferred
                        else f"Dokumentpaket wurde in '{target_path}' abgelegt."
                    ),
                    details={
                        "target_path": target_path,
                        "deferred": organization_deferred,
                        "moved_files": [str(path) for path in moved_files],
                    },
                )
            elif self._quality_requires_human_review(quality_report):
                self.report_workflow_status(
                    "archive",
                    "running",
                    "Qualitätskritisches Paket wird sicher für die Prüfung zurückgestellt.",
                )
                self.report_progress(0.96)
                stage_start = time.perf_counter()
                sorting_preview_path = self._resolve_exported_path(exported_paths, "pdf") or source_pdf_for_export
                moved_files = self._stage_unsorted_quality_review(
                    fused_text=fused_text,
                    metadata=metadata,
                    final_name=final_name,
                    artifact_paths=exported_paths,
                    quality_report=quality_report or {},
                    preview_pdf_path=sorting_preview_path,
                    is_docx=is_docx,
                )
                organize_audit = list(getattr(self, "_last_organize_audit", []) or [])
                manifest.record_stage(
                    "quality_review_staging",
                    "deferred",
                    artifacts={"moved_files": moved_files},
                    provenance={"target_path": "__archive_root__", "audit": organize_audit},
                )
                diagnostics.stage(
                    "quality_review_staging",
                    status="deferred",
                    start=stage_start,
                    target_path="__archive_root__",
                    moved_files=moved_files,
                    audit=organize_audit,
                )
                self.report_workflow_status(
                    "archive",
                    "warning",
                    "Dokumentpaket wurde sicher in die Review-Queue gestellt.",
                    details={"target_path": "_staging", "deferred": True},
                )
            else:
                self.report_workflow_status(
                    "archive",
                    "skipped",
                    "Automatische Unterordner-Ablage ist deaktiviert.",
                )

            # Reconcile every artifact after organization.  The exporter
            # initially writes into a publication area; moves and conflict
            # suffixes can change every concrete path.  Manifests, diagnostics,
            # the document index and sync must use the paths that actually
            # exist, not the pre-move placeholders.
            reconciled_paths = {}
            for artifact_key, artifact_path in (exported_paths or {}).items():
                resolved = self._resolve_exported_path(
                    exported_paths,
                    artifact_key,
                    moved_files,
                )
                reconciled_paths[artifact_key] = resolved or (
                    Path(artifact_path) if artifact_path and Path(artifact_path).exists() else None
                )
            exported_paths = reconciled_paths
            if self._reconcile_pdf_postflight_artifacts(quality_report, exported_paths):
                manifest.record_quality(quality_report)
            manifest.record_outputs(exported_paths)
            diagnostics.record_outputs(exported_paths)
            
            # Prüfen, ob dieser Lauf zurückgestellt wurde
            is_deferred = bool(getattr(self, "_current_organization_deferred", False))

            # Die konkreten lokalen Dateipfade fuer Upload und Cleanup aus dem Export-Ergebnis bestimmen.
            pdf_file = self._resolve_exported_path(exported_paths, "pdf") if not is_deferred else None
            docx_file = self._resolve_exported_path(exported_paths, "docx") if not is_deferred else None
            json_file = self._resolve_exported_path(exported_paths, "json") if not is_deferred else None

            # ── Remote-Synchronisierung ───────────────────────────────────
            sync_files = {
                "pdf_file": pdf_file,
                "docx_file": docx_file,
                "json_file": json_file,
                "is_docx": is_docx,
            }
            drive_uploads, drive_status = self._run_remote_sync(
                "google_drive",
                enabled=self.gdrive_enabled,
                upload=self._stage_gdrive_upload,
                manifest=manifest,
                diagnostics=diagnostics,
                is_deferred=is_deferred,
                target_path=target_path,
                **sync_files,
            )
            synology_uploads, synology_status = self._run_remote_sync(
                "synology",
                enabled=self.synology_enabled,
                upload=self._stage_synology_upload,
                manifest=manifest,
                diagnostics=diagnostics,
                is_deferred=is_deferred,
                target_path=target_path,
                **sync_files,
            )
            manifest.record_sync_uploads(
                enabled=(self.gdrive_enabled or self.synology_enabled) and not is_deferred,
                targets={
                    "google_drive": self.gdrive_enabled and not is_deferred,
                    "synology_webdav": self.synology_enabled and not is_deferred,
                },
                uploads=[*drive_uploads, *synology_uploads],
            )
            diagnostics.record_sync(
                enabled=(self.gdrive_enabled or self.synology_enabled) and not is_deferred,
                is_deferred=is_deferred,
                target_path=target_path,
                google_drive_uploads=drive_uploads,
                synology_uploads=synology_uploads,
            )

            # Cleanup von ungenutzten Dateien
            if not is_deferred:
                try:
                    if docx_file and docx_file.exists() and not self.save_docx_enabled:
                        docx_file.unlink()
                        exported_paths["docx"] = None
                except Exception as cleanup_err:
                    logger.warning(f"Konnte DOCX nicht löschen: {cleanup_err}")
                    
                if not self.save_json_enabled and json_file and json_file.exists():
                    try:
                        json_file.unlink()
                        exported_paths["json"] = None
                        self.log("Lokale JSON-Begleitdatei gelöscht (nur für Upload generiert).")
                    except Exception as cleanup_err:
                        logger.warning(f"Konnte JSON nicht löschen: {cleanup_err}")

            manifest.record_outputs(exported_paths)
            diagnostics.record_outputs(exported_paths)
            
            begleit_dir = self.config.final_dir / "begleitdateien"
            if begleit_dir.exists() and not any(begleit_dir.iterdir()):
                try:
                    begleit_dir.rmdir()
                except Exception:
                    pass

            title = metadata.get("title", "")
            tags = metadata_tags_text(metadata)
            if title or tags:
                self.log(f"-> Titel: {title} | Typ: {metadata.get('document_type', '')} | Tags: {tags}")

            final_job_status = self._phase_finalize_job(
                job_id=job_id,
                filename=filename,
                final_name=final_name,
                target_path=target_path,
                metadata=metadata,
                quality_report=quality_report,
                exported_paths=exported_paths,
                is_deferred=is_deferred,
                drive_status=drive_status,
                synology_status=synology_status,
                source_sha256=source_sha256,
                manifest=manifest,
                diagnostics=diagnostics,
                local_store=local_store,
                job_history=job_history,
            )


            self.report_progress(1.0)
            completion_state = (
                "success"
                if final_job_status == "completed"
                else "error"
                if final_job_status in {"failed", "sync_failed"}
                else "warning"
            )
            self.report_workflow_status(
                "complete",
                completion_state,
                (
                    "Verarbeitung und alle aktivierten Schritte wurden erfolgreich abgeschlossen."
                    if completion_state == "success"
                    else "Lokale Verarbeitung abgeschlossen; mindestens eine Synchronisierung ist fehlgeschlagen."
                    if final_job_status == "sync_failed"
                    else "Verarbeitung abgeschlossen; eine Prüfung oder Warnung ist noch offen."
                ),
                details={"job_status": final_job_status, "review_required": is_deferred},
            )
            self.log(f"{'─' * 50}\nVerarbeitung abgeschlossen: {filename} ({final_job_status})\n")
            return {
                "status": final_job_status,
                "job_id": job_id,
                "source_name": filename,
                "final_name": final_name,
                "target_path": target_path,
                "review_required": is_deferred,
                "outputs": {
                    key: str(value) if value else None
                    for key, value in (exported_paths or {}).items()
                },
                "sync": {
                    "google_drive": {"status": drive_status, "uploads": drive_uploads},
                    "synology": {"status": synology_status, "uploads": synology_uploads},
                },
            }

        except Exception as e:
            logger.exception(f"Schwerwiegender Fehler bei {filename}")
            self.log(f"FEHLER bei {filename}: {e}")
            active_step = str(getattr(self, "_active_workflow_step", "") or "")
            if active_step:
                self.report_workflow_status(
                    active_step,
                    "error",
                    f"Schritt fehlgeschlagen: {e}",
                )
            self.report_workflow_status(
                "complete",
                "error",
                f"Verarbeitung fehlgeschlagen: {e}",
                details={"job_status": "failed"},
            )
            error_preserve_paths = []
            try:
                evidence_dir = self.config.error_dir / job_id
                evidence_dir.mkdir(parents=True, exist_ok=True)
                if diagnostics is not None:
                    diagnostics.warn("Schwerwiegender Pipeline-Fehler", error=str(e))
                    diagnostics.event("job_failed", error=str(e))
                    debug_report_path = evidence_dir / "debug_report.json"
                    diagnostics.write_copy(debug_report_path)
                    error_preserve_paths.append(debug_report_path)
                if manifest is not None:
                    manifest.record_stage("process_file", "failed", warnings=[str(e)])
                    manifest.finalize("failed", error=str(e))
                    error_manifest = evidence_dir / "job_manifest.json"
                    manifest.write_copy(error_manifest)
                    error_preserve_paths.append(error_manifest)
            except Exception as manifest_err:
                logger.warning(f"Fehler-Manifest konnte nicht geschrieben werden: {manifest_err}")
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
                local_store.update_job(
                    job_id,
                    "failed",
                    final_name=final_name,
                    target_path=target_path,
                    metadata=metadata,
                    error=str(e),
                )
            except Exception as store_err:
                logger.warning(f"Persistenter Jobstatus konnte nicht geschrieben werden: {store_err}")
            try:
                if original_path.exists():
                    evidence_dir = self.config.error_dir / job_id
                    evidence_dir.mkdir(parents=True, exist_ok=True)
                    error_original_path = unique_path_for(evidence_dir, original_path)
                    shutil.move(str(original_path), str(error_original_path))
                    error_preserve_paths.append(error_original_path)
            except Exception:
                logger.exception("Konnte Fehlerdatei nicht verschieben")
            self.report_progress(0.0)
            return {
                "status": "failed",
                "job_id": job_id,
                "source_name": filename,
                "final_name": final_name,
                "target_path": target_path,
                "error": str(e),
            }

        finally:
            self._current_job_id = ""
            self._current_source_name = ""
            self._active_workflow_step = ""
            if work_dir.exists():
                try:
                    if not remove_directory_tree(work_dir):
                        logger.warning(
                            "Arbeitsverzeichnis konnte nicht gelöscht werden und bleibt zurück: %s",
                            work_dir,
                        )
                except Exception as e:
                    logger.warning(f"Arbeitsverzeichnis konnte nicht gelöscht werden: {e}")
