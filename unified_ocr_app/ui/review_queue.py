"""Compact, single-window review queue for staged OCR packages."""

from __future__ import annotations

import threading
from collections import Counter
from pathlib import Path
from tkinter import messagebox
from typing import Callable

import customtkinter as ctk

from core.error_messages import friendly_error_message
from core.review_service import ReviewResolutionError
from ui.pdf_preview import PDFPreviewFrame


KIND_LABELS = {
    "ocr_quality": "OCR-Qualität prüfen",
    "sorting_uncertain": "Ablageziel prüfen",
    "sorting_confirm": "Ablageziel bestätigen",
    "new_path": "Neuen Archivpfad prüfen",
    "staging": "Zurückgestelltes Paket",
}

FIELD_LABELS = {
    "document_date": "Dokumentdatum",
    "document_type": "Dokumenttyp",
    "title": "Titel",
    "tags": "Tags",
    "issuer": "Aussteller",
    "recipient": "Empfänger",
    "owner": "Akteninhaber / Eigentümer",
    "amount": "Betrag",
    "currency": "Währung",
    "language": "Sprache",
    "reference_ids": "Referenzen",
    "period": "Zeitraum",
}

REASON_LABELS = {
    "metadata_values_unverified": "Metadaten-Vorschläge sind noch nicht belegt",
    "missing_la_code": "Dokumenttyp oder Ablageregel ist unklar",
    "missing_amount": "Betrag konnte nicht sicher erkannt werden",
    "missing_date": "Dokumentdatum konnte nicht sicher erkannt werden",
    "missing_document_type": "Dokumenttyp konnte nicht sicher erkannt werden",
    "missing_issuer": "Aussteller konnte nicht sicher erkannt werden",
    "missing_recipient": "Empfänger konnte nicht sicher erkannt werden",
    "missing_title": "Titel konnte nicht sicher erkannt werden",
    "missing_tags": "Tags konnten nicht sicher erkannt werden",
    "unsupported_amount": "Betrag ist nicht ausreichend belegt",
    "amount_source_conflict": "Mehrere Betragsangaben widersprechen sich",
    "la_code_source_conflict": "Angaben zum Dokumenttyp widersprechen sich",
    "low_ocr_confidence": "OCR-Text hat eine niedrige Erkennungssicherheit",
    "garbled_text": "OCR-Text enthält möglicherweise fehlerhafte Zeichen",
    "short_text": "Es wurde nur wenig Text erkannt",
    "suspicious_characters": "OCR-Text enthält auffällige Zeichen",
    "digit_loss": "Ziffern könnten im OCR-Text fehlen",
    "table_line_loss": "Tabellenzeilen könnten im OCR-Text fehlen",
    "text_coverage_loss": "Teile des erkannten Texts könnten fehlen",
    "text_coverage_reduced": "Der OCR-Text könnte unvollständig sein",
    "text_expansion_unverified": "Zusätzlicher OCR-Text muss geprüft werden",
}

IMPORTANT_FIELDS = (
    ("document_date", "Dokumentdatum (JJJJ-MM-TT)"),
    ("document_type", "Dokumenttyp"),
    ("title", "Titel"),
    ("issuer", "Aussteller"),
    ("recipient", "Empfänger"),
    ("amount", "Betrag"),
    ("currency", "Währung (z. B. EUR)"),
    ("tags", "Tags (durch Komma getrennt)"),
)

OTHER_FIELDS = (
    ("owner", "Akteninhaber / Eigentümer"),
    ("language", "Sprache (z. B. de)"),
    ("reference_ids", "Referenzen (mit Semikolon trennen)"),
    ("period_start", "Zeitraum von (JJJJ-MM-TT)"),
    ("period_end", "Zeitraum bis (JJJJ-MM-TT)"),
    ("period_label", "Zeitraum-Bezeichnung"),
)


def _quality(item: dict) -> dict:
    value = item.get("quality")
    return value if isinstance(value, dict) else {}


def flagged_metadata_fields(item: dict) -> set[str]:
    """Return metadata keys that the evidence gate marked for human review."""

    quality = _quality(item)
    evidence = quality.get("metadata_evidence")
    evidence = evidence if isinstance(evidence, dict) else {}
    fields = evidence.get("fields")
    fields = fields if isinstance(fields, dict) else {}
    flagged = {
        str(name)
        for name, status in fields.items()
        if isinstance(status, dict)
        and str(status.get("status") or "").casefold() in {"unverified", "unknown"}
    }

    unverified = evidence.get("unverified_fields")
    if isinstance(unverified, list):
        for entry in unverified:
            if isinstance(entry, dict):
                name = entry.get("field") or entry.get("name") or entry.get("field_type")
            else:
                name = entry
            if name:
                flagged.add(str(name))

    reasons = quality.get("review_reasons")
    if isinstance(reasons, list):
        for reason in reasons:
            if not isinstance(reason, dict):
                continue
            field_name = reason.get("field_type") or reason.get("field")
            if field_name and str(field_name) in FIELD_LABELS:
                flagged.add(str(field_name))
    return flagged


def grouped_review_reasons(item: dict) -> list[str]:
    """Create a short, de-duplicated review summary for the editor."""

    quality = _quality(item)
    result: list[str] = []
    flagged = flagged_metadata_fields(item)
    if flagged:
        names = [FIELD_LABELS.get(name, name.replace("_", " ")) for name in FIELD_LABELS if name in flagged]
        names.extend(
            sorted(
                name.replace("_", " ")
                for name in flagged
                if name not in FIELD_LABELS
            )
        )
        result.append("Markierte Angaben prüfen: " + ", ".join(names))

    labels: list[str] = []
    reasons = quality.get("review_reasons")
    if isinstance(reasons, list):
        for reason in reasons:
            if isinstance(reason, dict):
                code = str(reason.get("code") or "").strip()
                if code == "metadata_values_unverified" and flagged:
                    continue
                label = REASON_LABELS.get(code)
                if not label:
                    action = str(reason.get("action") or "").strip()
                    label = action or code.replace("_", " ")
            else:
                label = str(reason).strip()
            if label:
                labels.append(label)

    if not labels and not result:
        warnings = quality.get("warnings")
        if isinstance(warnings, list):
            labels.extend(str(warning).strip() for warning in warnings if str(warning).strip())

    counts = Counter(labels)
    for label, count in counts.items():
        result.append(f"{label} ({count}×)" if count > 1 else label)
    return result or ["Vorschlag kurz mit dem Dokument vergleichen."]


def review_item_sort_key(item: dict) -> tuple[str, int]:
    """Sort newer review work before older imported queue rows."""

    try:
        item_id = int(item.get("id") or 0)
    except (TypeError, ValueError):
        item_id = 0
    return str(item.get("created_at") or item.get("updated_at") or ""), item_id


def _reference_ids_text(value) -> str:
    if not isinstance(value, list):
        return str(value or "")
    parts = []
    for entry in value:
        if isinstance(entry, dict):
            prefix = str(entry.get("type") or "").strip()
            identifier = str(entry.get("value") or "").strip()
            text = f"{prefix}: {identifier}" if prefix and identifier else identifier
        else:
            text = str(entry).strip()
        if text:
            parts.append(text)
    return "; ".join(parts)


def _shorten(value, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: max(1, limit - 1)].rstrip() + "…"


class ReviewQueueWindow(ctk.CTkToplevel):
    """A reusable non-modal queue with preview and editor in one window."""

    def __init__(
        self,
        master,
        service,
        *,
        sync_runner_factory: Callable[[], object],
        sync_artifacts: Callable[[object, dict, dict, object], object],
        dashboard_refresh: Callable[[], None] | None = None,
        remote_sync_enabled: Callable[[], bool] | None = None,
        on_close: Callable[["ReviewQueueWindow"], None] | None = None,
    ):
        super().__init__(master)
        self.withdraw()
        self.service = service
        self._sync_runner_factory = sync_runner_factory
        self._sync_artifacts = sync_artifacts
        self._dashboard_refresh = dashboard_refresh or (lambda: None)
        self._remote_sync_enabled = remote_sync_enabled or (lambda: False)
        self._on_close_callback = on_close
        self._destroying = False
        self._resolving = False
        self._items: list[dict] = []
        self._readiness: dict[int, tuple[bool, str]] = {}
        self._selected_item: dict | None = None
        self._metadata_vars: dict[str, ctk.StringVar] = {}
        self._target_var: ctk.StringVar | None = None
        self._baseline_snapshot = None
        self._preview_jobs: list[str] = []
        self._original_fused_text = ""
        self._publish_to_root = False

        try:
            self.title("Prüfungen")
            self.geometry("1500x840")
            self.minsize(1120, 700)
            self.transient(master)
            self.protocol("WM_DELETE_WINDOW", self.request_close)
            self.bind("<Escape>", lambda _event: self.request_close())
            self._build_layout()
            self.refresh_items()
            self.update_idletasks()
            self._place_on_screen()
            self.deiconify()
            self.lift()
            self.focus_force()
        except Exception:
            self._destroy_after_build_failure()
            raise

    @property
    def selected_item_id(self) -> int | None:
        if not self._selected_item:
            return None
        try:
            return int(self._selected_item.get("id"))
        except (TypeError, ValueError):
            return None

    def present(self) -> None:
        """Bring the existing queue forward instead of opening a duplicate."""

        try:
            if self.state() == "iconic":
                self.state("normal")
            self.deiconify()
            self.lift()
            self.focus_force()
        except Exception:
            return

    def _place_on_screen(self) -> None:
        """Keep the fixed action bar visible even after Windows cascades dialogs."""

        screen_width = max(900, int(self.winfo_screenwidth()))
        screen_height = max(700, int(self.winfo_screenheight()))
        width = min(1500, max(900, screen_width - 80))
        height = min(840, max(680, screen_height - 100))
        x = max(20, (screen_width - width) // 2)
        y = max(20, (screen_height - height) // 2)
        self.minsize(min(1120, width), min(700, height))
        self.geometry(f"{width}x{height}+{x}+{y}")

    def _build_layout(self) -> None:
        self.grid_columnconfigure(0, weight=0, minsize=300)
        self.grid_columnconfigure(1, weight=3, minsize=430)
        self.grid_columnconfigure(2, weight=2, minsize=390)
        self.grid_rowconfigure(1, weight=1)

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, columnspan=3, padx=18, pady=(14, 8), sticky="ew")
        header.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            header,
            text="Prüfungen",
            font=ctk.CTkFont(size=22, weight="bold"),
        ).grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(
            header,
            text="Vorschlag prüfen, bei Bedarf direkt ändern und anschließend ablegen.",
            text_color=("gray35", "gray70"),
        ).grid(row=1, column=0, pady=(2, 0), sticky="w")
        ctk.CTkButton(header, text="Neu laden", width=100, command=self.request_refresh).grid(
            row=0, column=1, rowspan=2, padx=(8, 6)
        )
        ctk.CTkButton(
            header,
            text="Schließen",
            width=100,
            fg_color=("#E5E7EB", "#374151"),
            hover_color=("#D1D5DB", "#4B5563"),
            text_color=("#111827", "#F9FAFB"),
            border_width=1,
            border_color=("#9CA3AF", "#6B7280"),
            command=self.request_close,
        ).grid(row=0, column=2, rowspan=2)

        queue_panel = ctk.CTkFrame(self)
        queue_panel.grid(row=1, column=0, padx=(18, 7), pady=(0, 16), sticky="nsew")
        queue_panel.grid_columnconfigure(0, weight=1)
        queue_panel.grid_rowconfigure(2, weight=1)
        queue_title = ctk.CTkFrame(queue_panel, fg_color="transparent")
        queue_title.grid(row=0, column=0, padx=12, pady=(12, 2), sticky="ew")
        queue_title.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            queue_title,
            text="Offene Prüfungen",
            font=ctk.CTkFont(size=15, weight="bold"),
        ).grid(row=0, column=0, sticky="w")
        self._count_var = ctk.StringVar(value="0 offen")
        ctk.CTkLabel(queue_title, textvariable=self._count_var, text_color="gray").grid(
            row=0, column=1, sticky="e"
        )
        ctk.CTkLabel(
            queue_panel,
            text="Neueste prüfbare Dokumente stehen oben.",
            text_color=("gray35", "gray70"),
            anchor="w",
        ).grid(row=1, column=0, padx=12, pady=(0, 6), sticky="ew")
        self._queue_scroll = ctk.CTkScrollableFrame(queue_panel, fg_color="transparent")
        self._queue_scroll.grid(row=2, column=0, padx=6, pady=(0, 8), sticky="nsew")
        self._queue_scroll.grid_columnconfigure(0, weight=1)

        preview_panel = ctk.CTkFrame(self)
        preview_panel.grid(row=1, column=1, padx=7, pady=(0, 16), sticky="nsew")
        preview_panel.grid_columnconfigure(0, weight=1)
        preview_panel.grid_rowconfigure(1, weight=1)
        ctk.CTkLabel(
            preview_panel,
            text="Dokumentvorschau",
            font=ctk.CTkFont(size=15, weight="bold"),
        ).grid(row=0, column=0, padx=12, pady=(12, 6), sticky="w")
        self._preview_host = ctk.CTkFrame(preview_panel, fg_color="transparent")
        self._preview_host.grid(row=1, column=0, padx=8, pady=(0, 8), sticky="nsew")
        self._preview_host.grid_columnconfigure(0, weight=1)
        self._preview_host.grid_rowconfigure(0, weight=1)

        editor_panel = ctk.CTkFrame(self)
        editor_panel.grid(row=1, column=2, padx=(7, 18), pady=(0, 16), sticky="nsew")
        editor_panel.grid_columnconfigure(0, weight=1)
        editor_panel.grid_rowconfigure(3, weight=1)
        self._document_title_var = ctk.StringVar(value="Prüfung auswählen")
        ctk.CTkLabel(
            editor_panel,
            textvariable=self._document_title_var,
            font=ctk.CTkFont(size=16, weight="bold"),
            anchor="w",
            justify="left",
            wraplength=390,
        ).grid(row=0, column=0, padx=12, pady=(12, 2), sticky="ew")
        self._quality_var = ctk.StringVar(value="")
        ctk.CTkLabel(
            editor_panel,
            textvariable=self._quality_var,
            text_color=("gray35", "gray70"),
            anchor="w",
        ).grid(row=1, column=0, padx=12, pady=(0, 5), sticky="ew")
        self._summary_var = ctk.StringVar(value="Links eine Prüfung auswählen.")
        summary = ctk.CTkFrame(
            editor_panel,
            border_width=1,
            border_color=("#D97706", "#F59E0B"),
        )
        summary.grid(row=2, column=0, padx=12, pady=(3, 8), sticky="ew")
        ctk.CTkLabel(
            summary,
            text="Das ist zu prüfen",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color=("#92400E", "#FBBF24"),
        ).grid(row=0, column=0, padx=10, pady=(8, 1), sticky="w")
        ctk.CTkLabel(
            summary,
            textvariable=self._summary_var,
            justify="left",
            anchor="w",
            wraplength=380,
        ).grid(row=1, column=0, padx=10, pady=(1, 8), sticky="ew")

        self._editor_tabs = ctk.CTkTabview(editor_panel)
        self._editor_tabs.grid(row=3, column=0, padx=8, pady=(0, 6), sticky="nsew")
        for tab_name in ("Wichtig", "Weitere Angaben", "Volltext"):
            self._editor_tabs.add(tab_name)
            tab = self._editor_tabs.tab(tab_name)
            tab.grid_columnconfigure(0, weight=1)
            tab.grid_rowconfigure(0, weight=1)
        self._important_scroll = ctk.CTkScrollableFrame(
            self._editor_tabs.tab("Wichtig"), fg_color="transparent"
        )
        self._other_scroll = ctk.CTkScrollableFrame(
            self._editor_tabs.tab("Weitere Angaben"), fg_color="transparent"
        )
        self._text_scroll = ctk.CTkScrollableFrame(
            self._editor_tabs.tab("Volltext"), fg_color="transparent"
        )
        for frame in (self._important_scroll, self._other_scroll, self._text_scroll):
            frame.grid(row=0, column=0, sticky="nsew")
            frame.grid_columnconfigure(0, weight=1)

        action = ctk.CTkFrame(editor_panel)
        action.grid(row=4, column=0, padx=12, pady=(2, 12), sticky="ew")
        action.grid_columnconfigure((0, 1), weight=1)
        self._action_status_var = ctk.StringVar(value="Eine Prüfung auswählen.")
        self._action_status_label = ctk.CTkLabel(
            action,
            textvariable=self._action_status_var,
            justify="left",
            anchor="w",
            wraplength=390,
        )
        self._action_status_label.grid(
            row=0, column=0, columnspan=2, padx=10, pady=(8, 5), sticky="ew"
        )
        self._reset_button = ctk.CTkButton(
            action,
            text="Vorschlag wiederherstellen",
            command=self.reset_selected,
            state="disabled",
            fg_color=("#E5E7EB", "#374151"),
            hover_color=("#D1D5DB", "#4B5563"),
            text_color=("#111827", "#F9FAFB"),
            border_width=1,
            border_color=("#9CA3AF", "#6B7280"),
        )
        self._reset_button.grid(row=1, column=0, padx=(10, 5), pady=(0, 10), sticky="ew")
        self._resolve_button = ctk.CTkButton(
            action,
            text=self._resolve_button_text(),
            command=self.resolve_selected,
            state="disabled",
            fg_color="#15803D",
            hover_color="#166534",
            height=44,
        )
        self._resolve_button.grid(row=1, column=1, padx=(5, 10), pady=(0, 10), sticky="ew")

    def _resolve_button_text(self) -> str:
        try:
            remote = bool(self._remote_sync_enabled())
        except Exception:
            remote = False
        return (
            "Bestätigen, ablegen & synchronisieren"
            if remote
            else "Angaben bestätigen & Dokument ablegen"
        )

    def request_refresh(self) -> None:
        if self._resolving:
            return
        if self.is_dirty() and not messagebox.askyesno(
            "Änderungen verwerfen?",
            "Nicht gespeicherte Änderungen verwerfen und die Prüfungen neu laden?",
            parent=self,
        ):
            return
        self.refresh_items()

    def refresh_items(self) -> None:
        previous_id = self.selected_item_id
        try:
            items = list(self.service.list_open(limit=200))
        except Exception as exc:
            self._set_status(f"Prüfungen konnten nicht geladen werden: {exc}", error=True)
            messagebox.showerror("Prüfungen konnten nicht geladen werden", str(exc), parent=self)
            return
        self._items = sorted(items, key=review_item_sort_key, reverse=True)
        self._readiness = {}
        for item in self._items:
            try:
                ready, reason = self.service.review_readiness(item)
            except Exception as exc:
                ready, reason = False, str(exc)
            try:
                self._readiness[int(item.get("id"))] = (bool(ready), str(reason or ""))
            except (TypeError, ValueError):
                continue

        self._count_var.set(f"{len(self._items)} offen")
        ids = {int(item.get("id")) for item in self._items if item.get("id") is not None}
        desired_id = previous_id if previous_id in ids else None
        if desired_id is None:
            desired_id = next(
                (
                    int(item["id"])
                    for item in self._items
                    if self._readiness.get(int(item.get("id") or 0), (False, ""))[0]
                ),
                int(self._items[0]["id"]) if self._items else None,
            )
        self._selected_item = None
        self._render_queue()
        if desired_id is not None:
            self.select_item(desired_id, force=True)
        else:
            self._show_empty_state()

    def _render_queue(self) -> None:
        for child in self._queue_scroll.winfo_children():
            child.destroy()
        if not self._items:
            ctk.CTkLabel(
                self._queue_scroll,
                text="Keine offenen Prüfungen.\nAlle Dokumente sind entschieden.",
                justify="center",
                text_color=("gray35", "gray70"),
            ).grid(row=0, column=0, padx=10, pady=30)
            return

        selected_id = self.selected_item_id
        for row, item in enumerate(self._items):
            item_id = int(item.get("id") or 0)
            quality = _quality(item)
            score = quality.get("quality_score")
            quality_text = str(quality.get("quality_status") or "offen")
            if score is not None:
                quality_text += f" · {score}/100"
            payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
            target = (
                "Archivwurzel"
                if payload.get("organize_enabled") is False
                else item.get("proposed_path") or "Kein Ziel vorgeschlagen"
            )
            ready, _reason = self._readiness.get(item_id, (False, ""))
            readiness_text = "" if ready else "\nDokumentdateien fehlen"
            text = (
                f"{_shorten(item.get('source_name') or 'Unbenanntes Dokument', 34)}\n"
                f"{KIND_LABELS.get(item.get('kind'), item.get('kind') or 'Prüfung')} · {quality_text}\n"
                f"Ziel: {_shorten(target, 38)}{readiness_text}"
            )
            selected = selected_id == item_id
            button = ctk.CTkButton(
                self._queue_scroll,
                text=text,
                command=lambda current_id=item_id: self.select_item(current_id),
                anchor="w",
                height=86 if ready else 105,
                border_width=2 if selected else 1,
                border_color="#2563EB" if selected else ("gray65", "gray35"),
                fg_color=("#DBEAFE", "#1E3A5F") if selected else ("gray88", "gray22"),
                hover_color=("#BFDBFE", "#274C77"),
                text_color=("#111827", "#F9FAFB"),
            )
            button.grid(row=row, column=0, padx=4, pady=4, sticky="ew")

    def select_item(self, item_id: int, *, force: bool = False) -> None:
        if self._resolving:
            return
        if self.selected_item_id == item_id and not force:
            return
        if not force and self.is_dirty() and not messagebox.askyesno(
            "Änderungen verwerfen?",
            "Die Änderungen an dieser Prüfung wurden noch nicht abgelegt. Verwerfen?",
            parent=self,
        ):
            return
        item = next((entry for entry in self._items if int(entry.get("id") or 0) == item_id), None)
        if item is None:
            return
        self._selected_item = item
        self._render_queue()
        self._render_preview(item)
        self._render_editor(item)

    def _cancel_preview_jobs(self) -> None:
        for job in self._preview_jobs:
            try:
                self.after_cancel(job)
            except Exception:
                pass
        self._preview_jobs.clear()

    def _release_preview_documents(self) -> None:
        """Close every preview PDF handle before a review mutates its files."""
        self._cancel_preview_jobs()
        try:
            pending = list(self._preview_host.winfo_children())
        except Exception:
            pending = []
        while pending:
            widget = pending.pop()
            try:
                pending.extend(widget.winfo_children())
            except Exception:
                pass
            if isinstance(widget, PDFPreviewFrame):
                try:
                    widget.release_document()
                except Exception:
                    pass

    def _render_preview(self, item: dict) -> None:
        self._cancel_preview_jobs()
        for child in self._preview_host.winfo_children():
            child.destroy()
        try:
            preview_path = self.service.preview_path(item)
            original_path = self.service.original_path(item)
        except Exception:
            preview_path = None
            original_path = None
        distinct_original = bool(
            original_path
            and Path(original_path).suffix.casefold() == ".pdf"
            and preview_path
            and Path(original_path).resolve(strict=False) != Path(preview_path).resolve(strict=False)
        )
        if distinct_original:
            tabs = ctk.CTkTabview(self._preview_host)
            tabs.grid(row=0, column=0, sticky="nsew")
            for name in ("Original", "OCR-Ergebnis"):
                tabs.add(name)
                tabs.tab(name).grid_columnconfigure(0, weight=1)
                tabs.tab(name).grid_rowconfigure(0, weight=1)
            original_viewer = PDFPreviewFrame(tabs.tab("Original"))
            original_viewer.grid(row=0, column=0, sticky="nsew")
            ocr_viewer = PDFPreviewFrame(tabs.tab("OCR-Ergebnis"))
            ocr_viewer.grid(row=0, column=0, sticky="nsew")
            self._preview_jobs.append(self.after(60, original_viewer.load_pdf, str(original_path)))
            self._preview_jobs.append(self.after(90, ocr_viewer.load_pdf, str(preview_path)))
            return

        viewer = PDFPreviewFrame(self._preview_host)
        viewer.grid(row=0, column=0, sticky="nsew")
        if preview_path:
            self._preview_jobs.append(self.after(70, viewer.load_pdf, str(preview_path)))
        elif original_path and Path(original_path).suffix.casefold() == ".pdf":
            self._preview_jobs.append(self.after(70, viewer.load_pdf, str(original_path)))
        else:
            viewer.show_message("Keine PDF-Vorschau verfügbar.\nDie Dokumentdateien dieses Eintrags fehlen.")

    @staticmethod
    def _clear_frame(frame) -> None:
        for child in frame.winfo_children():
            child.destroy()

    def _render_editor(self, item: dict) -> None:
        for frame in (self._important_scroll, self._other_scroll, self._text_scroll):
            self._clear_frame(frame)

        ready, readiness_reason = self._readiness.get(
            int(item.get("id") or 0), (False, "")
        )
        quality = _quality(item)
        score = quality.get("quality_score")
        score_text = f" · {score}/100" if score is not None else ""
        self._document_title_var.set(
            f"#{item.get('id')}  {item.get('source_name') or 'Unbenanntes Dokument'}"
        )
        self._quality_var.set(
            f"{KIND_LABELS.get(item.get('kind'), item.get('kind') or 'Prüfung')}"
            f" · Qualität: {quality.get('quality_status') or 'nicht bewertet'}{score_text}"
        )
        reasons = (
            grouped_review_reasons(item)
            if ready
            else ["Dokumentdateien fehlen. Dieser Eintrag kann nicht abgeschlossen werden."]
        )
        visible_reasons = reasons[:4]
        if len(reasons) > 4:
            visible_reasons.append(f"{len(reasons) - 4} weitere Hinweise zusammengefasst")
        self._summary_var.set("\n".join(f"• {reason}" for reason in visible_reasons))

        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        period = metadata.get("period") if isinstance(metadata.get("period"), dict) else {}
        tags = metadata.get("tags")
        tags_text = ", ".join(str(tag) for tag in tags) if isinstance(tags, list) else str(tags or "")
        values = {
            "document_date": str(metadata.get("document_date") or ""),
            "document_type": str(metadata.get("document_type") or ""),
            "title": str(metadata.get("title") or metadata.get("filename_title") or ""),
            "tags": tags_text,
            "issuer": str(metadata.get("issuer") or ""),
            "recipient": str(metadata.get("recipient") or ""),
            "owner": str(metadata.get("owner") or ""),
            "amount": str(metadata.get("amount") or ""),
            "currency": str(metadata.get("currency") or ""),
            "language": str(metadata.get("language") or ""),
            "reference_ids": _reference_ids_text(metadata.get("reference_ids")),
            "period_start": str(period.get("start") or ""),
            "period_end": str(period.get("end") or ""),
            "period_label": str(period.get("label") or ""),
        }
        self._metadata_vars = {
            name: ctk.StringVar(master=self, value=value) for name, value in values.items()
        }
        flagged = flagged_metadata_fields(item)
        self._build_fields(self._important_scroll, IMPORTANT_FIELDS, flagged)

        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        self._publish_to_root = payload.get("organize_enabled") is False
        if self._publish_to_root:
            proposed = "Archivwurzel (ohne Sortierung)"
            known_paths = [proposed]
        else:
            try:
                known_paths = list(self.service.known_paths())
            except Exception:
                known_paths = ["Sonstiges"]
            proposed = str(item.get("proposed_path") or (known_paths[0] if known_paths else "Sonstiges"))
            known_paths = list(dict.fromkeys([proposed, *known_paths]))
        target_row = len(IMPORTANT_FIELDS) * 2
        ctk.CTkLabel(
            self._important_scroll,
            text="Ablageziel",
            font=ctk.CTkFont(weight="bold"),
        ).grid(row=target_row, column=0, padx=6, pady=(8, 2), sticky="w")
        self._target_var = ctk.StringVar(master=self, value=proposed)
        ctk.CTkComboBox(
            self._important_scroll,
            variable=self._target_var,
            values=known_paths or ["Sonstiges"],
            state="disabled" if self._publish_to_root else "normal",
        ).grid(row=target_row + 1, column=0, padx=6, pady=(0, 8), sticky="ew")

        self._build_fields(self._other_scroll, OTHER_FIELDS, flagged)
        note_row = len(OTHER_FIELDS) * 2
        ctk.CTkLabel(self._other_scroll, text="Prüfnotiz (optional)").grid(
            row=note_row, column=0, padx=6, pady=(8, 2), sticky="w"
        )
        self._note_box = ctk.CTkTextbox(self._other_scroll, height=90, wrap="word")
        self._note_box.grid(row=note_row + 1, column=0, padx=6, pady=(0, 8), sticky="ew")

        ctk.CTkLabel(
            self._text_scroll,
            text=(
                "Nur ändern, wenn der erkannte Text nicht zum Dokument passt. "
                "Der maschinelle OCR-Layer im Archiv-PDF bleibt als Originalbefund erhalten."
            ),
            justify="left",
            anchor="w",
            wraplength=370,
            text_color=("gray35", "gray70"),
        ).grid(row=0, column=0, padx=6, pady=(5, 6), sticky="ew")
        self._original_fused_text = str(payload.get("fused_text") or "")
        self._text_box = ctk.CTkTextbox(self._text_scroll, height=360, wrap="word")
        self._text_box.grid(row=1, column=0, padx=6, pady=(0, 8), sticky="ew")
        if self._original_fused_text:
            self._text_box.insert("1.0", self._original_fused_text)

        if ready:
            self._resolve_button.configure(
                state="normal",
                text=self._resolve_button_text(),
                fg_color="#15803D",
                hover_color="#166534",
            )
        else:
            self._resolve_button.configure(
                state="disabled",
                text="Nicht abschließbar – Dateien fehlen",
                fg_color=("#D1D5DB", "#4B5563"),
                hover_color=("#D1D5DB", "#4B5563"),
            )
        self._reset_button.configure(state="normal")
        if ready:
            self._set_status(
                "Gelb markierte Angaben prüfen oder ändern. Der grüne Button bestätigt und legt das Dokument ab."
            )
        else:
            self._set_status(
                "Dieser veraltete Eintrag kann nicht abgeschlossen werden: "
                + (readiness_reason or "Dokumentdateien fehlen."),
                error=True,
            )
        self._baseline_snapshot = self._editor_snapshot()

    def _build_fields(self, frame, specs, flagged: set[str]) -> None:
        for index, (name, label_text) in enumerate(specs):
            evidence_name = "period" if name.startswith("period_") else name
            needs_review = evidence_name in flagged
            label = f"{label_text}  [prüfen]" if needs_review else label_text
            ctk.CTkLabel(
                frame,
                text=label,
                text_color=("#92400E", "#FBBF24") if needs_review else None,
            ).grid(row=index * 2, column=0, padx=6, pady=(5, 1), sticky="w")
            ctk.CTkEntry(frame, textvariable=self._metadata_vars[name]).grid(
                row=index * 2 + 1, column=0, padx=6, pady=(0, 3), sticky="ew"
            )

    def _editor_snapshot(self):
        if not self._selected_item or not self._target_var:
            return None
        try:
            return (
                tuple((name, variable.get()) for name, variable in sorted(self._metadata_vars.items())),
                self._target_var.get(),
                self._note_box.get("1.0", "end-1c"),
                self._text_box.get("1.0", "end-1c"),
            )
        except Exception:
            return None

    def is_dirty(self) -> bool:
        return self._baseline_snapshot is not None and self._editor_snapshot() != self._baseline_snapshot

    def reset_selected(self) -> None:
        if self._resolving or not self._selected_item:
            return
        self._render_editor(self._selected_item)
        self._set_status("Der gespeicherte Vorschlag wurde wiederhergestellt.")

    def _corrected_metadata(self) -> dict:
        metadata = dict(self._selected_item.get("metadata") or {})
        metadata.update(
            {
                "document_date": self._metadata_vars["document_date"].get().strip() or None,
                "document_type": self._metadata_vars["document_type"].get().strip(),
                "title": self._metadata_vars["title"].get().strip(),
                "tags": self._metadata_vars["tags"].get().strip(),
                "issuer": self._metadata_vars["issuer"].get().strip(),
                "recipient": self._metadata_vars["recipient"].get().strip(),
                "owner": self._metadata_vars["owner"].get().strip(),
                "amount": self._metadata_vars["amount"].get().strip(),
                "currency": self._metadata_vars["currency"].get().strip(),
                "language": self._metadata_vars["language"].get().strip(),
                "reference_ids": self._metadata_vars["reference_ids"].get().strip(),
                "period": {
                    "start": self._metadata_vars["period_start"].get().strip() or None,
                    "end": self._metadata_vars["period_end"].get().strip() or None,
                    "label": self._metadata_vars["period_label"].get().strip() or None,
                },
            }
        )
        return metadata

    def resolve_selected(self) -> None:
        if self._resolving or not self._selected_item:
            return
        item = self._selected_item
        item_id = int(item.get("id") or 0)
        ready, reason = self._readiness.get(item_id, (False, ""))
        if not ready:
            self._set_status(reason or "Dokumentdateien fehlen.", error=True)
            return
        target_path = "" if self._publish_to_root else str(self._target_var.get() or "").strip()
        if not self._publish_to_root and not target_path:
            self._set_status("Bitte ein Ablageziel auswählen oder eingeben.", error=True)
            return
        try:
            reviewed_text = self._text_box.get("1.0", "end-1c")
            review_note = self._note_box.get("1.0", "end-1c").strip()
            corrected_metadata = self._corrected_metadata()
            sync_runner = self._sync_runner_factory()
        except Exception as exc:
            error_text = friendly_error_message(
                exc, context="Die eingegebenen Werte bleiben geöffnet."
            )
            self._set_status(error_text, error=True)
            messagebox.showerror("Prüfung konnte nicht vorbereitet werden", error_text, parent=self)
            return

        post_publish_callback = None
        if getattr(sync_runner, "gdrive_enabled", False) or getattr(
            sync_runner, "synology_enabled", False
        ):
            post_publish_callback = lambda context: self._sync_artifacts(
                self.service.config,
                context,
                context.get("item") or item,
                sync_runner,
            )

        self._resolving = True
        self._resolve_button.configure(state="disabled", text="Ablage läuft …")
        self._reset_button.configure(state="disabled")
        self._set_status(
            "Das Dokument wird sicher abgelegt. Das Fenster bleibt bis zum Ergebnis geöffnet."
        )
        # PyMuPDF keeps the staged PDF open for page navigation.  Windows
        # refuses the atomic metadata rewrite while that handle is active
        # (WinError 5).  Release every preview handle on the Tk thread before
        # the background resolve/upload worker touches the package.
        self._release_preview_documents()

        def finish_error(exc: Exception) -> None:
            self._resolving = False
            try:
                if not self.winfo_exists():
                    return
            except Exception:
                return
            self._resolve_button.configure(state="normal", text=self._resolve_button_text())
            self._resolve_button.configure(fg_color="#15803D", hover_color="#166534")
            self._reset_button.configure(state="normal")
            if self.selected_item_id == item_id and self._selected_item:
                # The persisted operation failed after the preview handle was
                # released.  Restore the preview only once all file writes
                # have stopped, preserving the user's editor values.
                self._render_preview(self._selected_item)
            if isinstance(exc, ReviewResolutionError):
                error_text = str(exc)
            else:
                error_text = friendly_error_message(
                    exc,
                    context="Die Ablage wurde nicht abgeschlossen; die Prüfung bleibt sicher erhalten.",
                )
            self._set_status(error_text, error=True)
            messagebox.showerror("Dokument wurde nicht abgelegt", error_text, parent=self)

        def finish_success(result: dict) -> None:
            self._resolving = False
            destination = (
                "Archivwurzel"
                if self._publish_to_root
                else f"final/{result.get('target_path') or target_path}"
            )
            self.refresh_items()
            self._set_status(f"Dokument erfolgreich abgelegt: {destination}", success=True)
            try:
                self._dashboard_refresh()
            except Exception:
                pass

        def schedule(callback, *args) -> None:
            try:
                self.after(0, callback, *args)
            except Exception:
                return

        def worker() -> None:
            try:
                result = self.service.resolve(
                    item_id,
                    target_path,
                    quality_confirmed=True,
                    review_note=review_note,
                    corrected_text=(
                        reviewed_text
                        if self._original_fused_text or reviewed_text.strip()
                        else None
                    ),
                    corrected_metadata=corrected_metadata,
                    post_publish_callback=post_publish_callback,
                )
            except Exception as exc:
                schedule(finish_error, exc)
            else:
                schedule(finish_success, result)

        threading.Thread(target=worker, daemon=True).start()

    def _show_empty_state(self) -> None:
        self._selected_item = None
        self._document_title_var.set("Keine offene Prüfung")
        self._quality_var.set("")
        self._summary_var.set("Alle Dokumente sind entschieden.")
        for frame in (self._important_scroll, self._other_scroll, self._text_scroll):
            self._clear_frame(frame)
        self._cancel_preview_jobs()
        for child in self._preview_host.winfo_children():
            child.destroy()
        viewer = PDFPreviewFrame(self._preview_host)
        viewer.grid(row=0, column=0, sticky="nsew")
        viewer.show_message("Keine offene Prüfung.")
        self._resolve_button.configure(
            state="disabled",
            text="Keine offene Prüfung",
            fg_color=("#D1D5DB", "#4B5563"),
            hover_color=("#D1D5DB", "#4B5563"),
        )
        self._reset_button.configure(state="disabled")
        self._baseline_snapshot = None
        self._set_status("Keine offenen Prüfungen.")

    def _set_status(self, text: str, *, error: bool = False, success: bool = False) -> None:
        self._action_status_var.set(str(text))
        color = (
            ("#991B1B", "#FCA5A5")
            if error
            else ("#166534", "#86EFAC")
            if success
            else ("gray25", "gray75")
        )
        self._action_status_label.configure(text_color=color)

    def request_close(self) -> None:
        if self._resolving:
            messagebox.showinfo(
                "Ablage läuft",
                "Das Dokument wird gerade abgelegt. Das Fenster schließt nach Abschluss wieder normal.",
                parent=self,
            )
            return
        if self.is_dirty() and not messagebox.askyesno(
            "Änderungen verwerfen?",
            "Die Änderungen wurden noch nicht abgelegt. Fenster trotzdem schließen?",
            parent=self,
        ):
            return
        self.destroy()

    def _destroy_after_build_failure(self) -> None:
        self._cancel_preview_jobs()
        try:
            super().destroy()
        except Exception:
            try:
                self.tk.call("destroy", self._w)
            except Exception:
                pass

    def destroy(self) -> None:
        if self._destroying:
            return
        self._destroying = True
        self._cancel_preview_jobs()
        try:
            super().destroy()
        finally:
            callback = self._on_close_callback
            self._on_close_callback = None
            if callback:
                try:
                    callback(self)
                except Exception:
                    pass
