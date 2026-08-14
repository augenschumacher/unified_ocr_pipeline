from __future__ import annotations

import tkinter as tk
from pathlib import Path

import customtkinter as ctk
from PIL import Image, ImageTk

try:
    import fitz
except Exception:
    fitz = None


class PDFPreviewFrame(ctk.CTkFrame):
    """Lazy, searchable PDF preview suited for review of large documents."""

    def __init__(self, master, **kwargs):
        super().__init__(master, **kwargs)
        self.doc = None
        self.current_page = 0
        self.pdf_path = None
        self.zoom = 1.0
        self.rotation = 0
        self._photo = None
        self._resize_job = None
        self._search_query = ""
        self._search_rects = []

        self.grid_rowconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=0)
        self.grid_rowconfigure(2, weight=0)
        self.grid_rowconfigure(3, weight=0)
        self.grid_columnconfigure(0, weight=1)

        canvas_frame = ctk.CTkFrame(self)
        canvas_frame.grid(row=0, column=0, padx=8, pady=(8, 4), sticky="nsew")
        canvas_frame.grid_rowconfigure(0, weight=1)
        canvas_frame.grid_columnconfigure(0, weight=1)

        self.canvas = tk.Canvas(
            canvas_frame,
            background="#202020",
            highlightthickness=0,
            takefocus=True,
        )
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.v_scroll = ctk.CTkScrollbar(canvas_frame, orientation="vertical", command=self.canvas.yview)
        self.v_scroll.grid(row=0, column=1, sticky="ns")
        self.h_scroll = ctk.CTkScrollbar(canvas_frame, orientation="horizontal", command=self.canvas.xview)
        self.h_scroll.grid(row=1, column=0, sticky="ew")
        self.canvas.configure(xscrollcommand=self.h_scroll.set, yscrollcommand=self.v_scroll.set)
        self.canvas.create_text(
            20,
            20,
            anchor="nw",
            fill="#d1d5db",
            text="Keine Vorschau verfügbar",
            tags=("message",),
        )

        search_frame = ctk.CTkFrame(self, fg_color="transparent")
        search_frame.grid(row=1, column=0, padx=10, pady=4, sticky="ew")
        search_frame.grid_columnconfigure(0, weight=1)
        self.search_var = ctk.StringVar(value="")
        self.search_entry = ctk.CTkEntry(
            search_frame,
            textvariable=self.search_var,
            placeholder_text="Text im PDF suchen…",
        )
        self.search_entry.grid(row=0, column=0, padx=(0, 6), sticky="ew")
        self.search_entry.bind("<Return>", lambda _event: self.find_next())
        ctk.CTkButton(search_frame, text="Weitersuchen", width=100, command=self.find_next).grid(row=0, column=1)
        self.search_status = ctk.CTkLabel(search_frame, text="", text_color="gray", width=120)
        self.search_status.grid(row=0, column=2, padx=(8, 0), sticky="w")

        controls = ctk.CTkFrame(self, fg_color="transparent")
        controls.grid(row=2, column=0, padx=10, pady=(2, 8), sticky="ew")
        controls.grid_columnconfigure(3, weight=1)

        self.prev_btn = ctk.CTkButton(controls, text="◀ Zurück", width=82, command=self.prev_page)
        self.prev_btn.grid(row=0, column=0, padx=(0, 5))
        self.page_var = ctk.StringVar(value="-")
        self.page_entry = ctk.CTkEntry(controls, textvariable=self.page_var, width=52, justify="center")
        self.page_entry.grid(row=0, column=1, padx=3)
        self.page_entry.bind("<Return>", self._go_to_page_from_entry)
        self.page_label = ctk.CTkLabel(controls, text="von -")
        self.page_label.grid(row=0, column=2, padx=(3, 8))
        self.next_btn = ctk.CTkButton(controls, text="Weiter ▶", width=82, command=self.next_page)
        self.next_btn.grid(row=0, column=3, padx=5, sticky="w")

        ctk.CTkButton(controls, text="−", width=34, command=self.zoom_out).grid(row=0, column=4, padx=2)
        self.zoom_label = ctk.CTkLabel(controls, text="100 %", width=58)
        self.zoom_label.grid(row=0, column=5, padx=2)
        ctk.CTkButton(controls, text="+", width=34, command=self.zoom_in).grid(row=0, column=6, padx=2)
        ctk.CTkButton(controls, text="Einpassen", width=76, command=self.fit_page).grid(row=0, column=7, padx=(6, 2))
        ctk.CTkButton(controls, text="Drehen ↻", width=76, command=self.rotate_clockwise).grid(row=0, column=8, padx=(2, 0))

        self.canvas.bind("<Configure>", self._on_resize)
        self.canvas.bind("<Left>", lambda _event: self.prev_page())
        self.canvas.bind("<Right>", lambda _event: self.next_page())
        self.canvas.bind("<Prior>", lambda _event: self.prev_page())
        self.canvas.bind("<Next>", lambda _event: self.next_page())
        self.canvas.bind("<Control-plus>", lambda _event: self.zoom_in())
        self.canvas.bind("<Control-minus>", lambda _event: self.zoom_out())
        self.canvas.bind("<F3>", lambda _event: self.find_next())

    def _on_resize(self, _event):
        if self._resize_job:
            self.after_cancel(self._resize_job)
        if self.doc is not None and not self.doc.is_closed:
            self._resize_job = self.after(120, self.show_page)

    def destroy(self):
        if self._resize_job:
            try:
                self.after_cancel(self._resize_job)
            except Exception:
                pass
        self._close_document()
        super().destroy()

    def _close_document(self):
        if self.doc is not None:
            try:
                if not self.doc.is_closed:
                    self.doc.close()
            except Exception:
                pass
        self.doc = None
        self._photo = None

    def release_document(self) -> None:
        """Release the PDF file handle while keeping the preview widget alive.

        Windows does not allow an archival PDF to be atomically replaced while
        PyMuPDF still has it open.  Review workflows call this immediately
        before persisting corrected metadata or moving the package.
        """
        if self._resize_job:
            try:
                self.after_cancel(self._resize_job)
            except Exception:
                pass
            self._resize_job = None
        self._close_document()
        try:
            self.prev_btn.configure(state="disabled")
            self.next_btn.configure(state="disabled")
            self.search_status.configure(text="Vorschau für Ablage freigegeben")
        except Exception:
            pass

    def _show_message(self, message: str):
        self.canvas.delete("all")
        self.canvas.create_text(20, 20, anchor="nw", fill="#d1d5db", text=message, tags=("message",))
        self.canvas.configure(scrollregion=(0, 0, 1, 1))
        self.page_var.set("-")
        self.page_label.configure(text="von -")

    def show_message(self, message: str):
        """Display a review-safe placeholder without reaching into internals."""
        self._show_message(message)

    def load_pdf(self, path):
        self._close_document()
        self.pdf_path = str(path) if path else None
        self.current_page = 0
        self.zoom = 1.0
        self.rotation = 0
        self._search_query = ""
        self._search_rects = []
        self.search_status.configure(text="")

        if fitz is None:
            self._show_message("PDF-Vorschau nicht verfügbar:\nPyMuPDF ist nicht installiert.")
            return
        if not path or not Path(path).is_file():
            self._show_message("PDF-Vorschau nicht verfügbar:\nDatei wurde nicht gefunden.")
            return
        if Path(path).suffix.lower() in {".docx", ".odt", ".doc", ".odoc"}:
            self._show_message(f"{Path(path).suffix[1:].upper()}-Dokument geladen.\nKeine visuelle Vorschau verfügbar.")
            return

        try:
            # Open lazily from disk instead of reading a potentially very large
            # archive PDF into memory in one operation.
            self.doc = fitz.open(str(path))
            if len(self.doc) == 0:
                raise ValueError("PDF enthält keine Seiten.")
            self.show_page()
            self.canvas.focus_set()
        except Exception as exc:
            self._close_document()
            self.pdf_path = None
            self._show_message(f"Fehler beim Laden der PDF:\n{exc}")

    def _render_matrix(self, page):
        available_width = max(180, self.canvas.winfo_width() - 20)
        available_height = max(180, self.canvas.winfo_height() - 20)
        rotated = self.rotation % 180 != 0
        page_width = float(page.rect.height if rotated else page.rect.width)
        page_height = float(page.rect.width if rotated else page.rect.height)
        fit_scale = min(available_width / max(page_width, 1), available_height / max(page_height, 1))
        scale = max(0.25, min(6.0, fit_scale * self.zoom))
        return fitz.Matrix(scale, scale).prerotate(self.rotation)

    def show_page(self):
        self._resize_job = None
        if self.doc is None or self.doc.is_closed:
            return
        try:
            self.current_page = max(0, min(self.current_page, len(self.doc) - 1))
            page = self.doc.load_page(self.current_page)
            matrix = self._render_matrix(page)
            pix = page.get_pixmap(matrix=matrix, alpha=False)
            image = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
            self._photo = ImageTk.PhotoImage(image)

            self.canvas.delete("all")
            canvas_width = max(1, self.canvas.winfo_width())
            canvas_height = max(1, self.canvas.winfo_height())
            x = max(0, (canvas_width - pix.width) // 2)
            y = max(0, (canvas_height - pix.height) // 2)
            self.canvas.create_image(x, y, image=self._photo, anchor="nw")

            for rect in self._search_rects:
                transformed = rect * matrix
                self.canvas.create_rectangle(
                    x + transformed.x0,
                    y + transformed.y0,
                    x + transformed.x1,
                    y + transformed.y1,
                    outline="#ef4444",
                    width=3,
                )

            self.canvas.configure(
                scrollregion=(0, 0, max(canvas_width, x + pix.width), max(canvas_height, y + pix.height))
            )
            self.page_var.set(str(self.current_page + 1))
            self.page_label.configure(text=f"von {len(self.doc)}")
            self.zoom_label.configure(text=f"{int(self.zoom * 100)} %")
            self.prev_btn.configure(state="normal" if self.current_page > 0 else "disabled")
            self.next_btn.configure(state="normal" if self.current_page < len(self.doc) - 1 else "disabled")
        except Exception as exc:
            self._show_message(f"Fehler beim Rendern der Seite:\n{exc}")

    def _go_to_page_from_entry(self, _event=None):
        if self.doc is None or self.doc.is_closed:
            return
        try:
            requested = int(self.page_var.get())
        except (TypeError, ValueError):
            self.page_var.set(str(self.current_page + 1))
            return
        self.current_page = max(0, min(requested - 1, len(self.doc) - 1))
        self._search_rects = []
        self.show_page()

    def prev_page(self):
        if self.doc is not None and not self.doc.is_closed and self.current_page > 0:
            self.current_page -= 1
            self._search_rects = []
            self.show_page()

    def next_page(self):
        if self.doc is not None and not self.doc.is_closed and self.current_page < len(self.doc) - 1:
            self.current_page += 1
            self._search_rects = []
            self.show_page()

    def zoom_in(self):
        self.zoom = min(4.0, round(self.zoom + 0.25, 2))
        self.show_page()

    def zoom_out(self):
        self.zoom = max(0.5, round(self.zoom - 0.25, 2))
        self.show_page()

    def fit_page(self):
        self.zoom = 1.0
        self.show_page()

    def rotate_clockwise(self):
        self.rotation = (self.rotation + 90) % 360
        self.show_page()

    def find_next(self):
        if self.doc is None or self.doc.is_closed:
            return
        query = self.search_var.get().strip()
        if not query:
            self.search_status.configure(text="Suchtext fehlt")
            return

        start = self.current_page + (1 if query == self._search_query and self._search_rects else 0)
        self._search_query = query
        for offset in range(len(self.doc)):
            page_index = (start + offset) % len(self.doc)
            rects = self.doc.load_page(page_index).search_for(query)
            if rects:
                self.current_page = page_index
                self._search_rects = list(rects)
                self.search_status.configure(text=f"{len(rects)} Treffer auf Seite {page_index + 1}")
                self.show_page()
                return
        self._search_rects = []
        self.search_status.configure(text="Kein Treffer")
        self.show_page()
