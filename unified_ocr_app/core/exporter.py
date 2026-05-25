"""Output generation for processed OCR jobs."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from core.docx_tools import save_markdown_as_docx
from core.ocr import inject_fused_text_and_metadata


class DocumentExporter:
    def __init__(
        self,
        *,
        config,
        output_format: str,
        docx_mode: str,
        save_docx_enabled: bool,
        save_json_enabled: bool,
        gdrive_enabled: bool,
        gdrive_upload_docx: bool,
        gdrive_upload_json: bool,
        log_callback,
        save_docx_func=save_markdown_as_docx,
        inject_pdf_func=inject_fused_text_and_metadata,
    ):
        self.config = config
        self.output_format = output_format
        self.docx_mode = docx_mode
        self.save_docx_enabled = save_docx_enabled
        self.save_json_enabled = save_json_enabled
        self.gdrive_enabled = gdrive_enabled
        self.gdrive_upload_docx = gdrive_upload_docx
        self.gdrive_upload_json = gdrive_upload_json
        self.log = log_callback
        self.save_docx_func = save_docx_func
        self.inject_pdf_func = inject_pdf_func

    def export(
        self,
        work_pdf: Path,
        fused_pages: dict,
        fused_text: str,
        final_name: str,
        metadata: dict,
        image_paths: list,
        quality_report: dict,
        *,
        is_docx: bool = False,
    ) -> dict:
        """Generate the configured local output files and return their paths."""
        created = {"pdf": None, "txt": None, "docx": None, "json": None}

        should_generate_json = self.save_json_enabled or (self.gdrive_enabled and self.gdrive_upload_json)
        if should_generate_json:
            begleit_dir = self.config.final_dir / "begleitdateien"
            begleit_dir.mkdir(parents=True, exist_ok=True)
            report_path = begleit_dir / f"{final_name}_quality_report.json"
            try:
                with open(report_path, "w", encoding="utf-8") as f:
                    json.dump(quality_report, f, indent=4, ensure_ascii=False)
                created["json"] = report_path
                self.log(f"-> Qualitätsbericht: begleitdateien/{report_path.name}")
            except Exception as e:
                self.log(f"Qualitätsbericht konnte nicht gespeichert werden: {e}")

        fmt = self.output_format

        if is_docx:
            orig_suffix = work_pdf.suffix.lower()
            docx_path = self.config.final_dir / f"{final_name}.docx"
            if orig_suffix == ".docx":
                try:
                    shutil.copy2(work_pdf, docx_path)
                    created["docx"] = docx_path
                    self.log(f"-> DOCX: {docx_path.name}")
                except Exception as e:
                    self.log(f"Konnte DOCX nicht kopieren: {e}")
            else:
                self.log(f"Konvertiere {orig_suffix.upper()[1:]} zu DOCX...")
                try:
                    saved = self.save_docx_func(
                        fused_text,
                        docx_path,
                        mode=self.docx_mode,
                        image_paths=image_paths,
                        quality_report=quality_report,
                    )
                    created["docx"] = saved
                    self.log(f"-> DOCX (konvertiert aus {orig_suffix.upper()[1:]}): {docx_path.name}")
                except Exception as e:
                    self.log(f"Konnte DOCX aus {orig_suffix.upper()[1:]} nicht generieren: {e}")
        else:
            if fmt in ("Nur PDF", "PDF und TXT", "PDF und DOCX"):
                pdf_path = self.config.final_dir / f"{final_name}.pdf"
                self.log("Erstelle durchsuchbare finale PDF...")
                self.inject_pdf_func(work_pdf, pdf_path, fused_pages, metadata)
                created["pdf"] = pdf_path
                self.log(f"-> PDF: {pdf_path.name}")

        if fmt in ("Nur TXT", "PDF und TXT") or (is_docx and fmt == "PDF und TXT"):
            txt_path = self.config.final_dir / f"{final_name}.txt"
            txt_path.write_text(fused_text, encoding="utf-8")
            created["txt"] = txt_path
            self.log(f"-> TXT: {txt_path.name}")

        if not is_docx:
            should_generate_docx = (
                (fmt in ("Nur DOCX", "PDF und DOCX") and self.save_docx_enabled)
                or (self.gdrive_enabled and self.gdrive_upload_docx)
            )
            if should_generate_docx:
                begleit_dir = self.config.final_dir / "begleitdateien"
                begleit_dir.mkdir(parents=True, exist_ok=True)
                docx_path = begleit_dir / f"{final_name}.docx"
                self.log(f"Erstelle DOCX (Modus: {self.docx_mode})...")
                saved = self.save_docx_func(
                    fused_text,
                    docx_path,
                    mode=self.docx_mode,
                    image_paths=image_paths,
                    quality_report=quality_report,
                )
                created["docx"] = saved
                self.log(f"-> DOCX: begleitdateien/{saved.name}")
            else:
                self.log("DOCX-Erstellung übersprungen (in den Einstellungen deaktiviert).")

        return created
