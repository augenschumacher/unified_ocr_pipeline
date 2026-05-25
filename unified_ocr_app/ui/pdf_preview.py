import customtkinter as ctk
from PIL import Image
import fitz


class PDFPreviewFrame(ctk.CTkFrame):
    """Renders a page-by-page preview of a PDF file using PyMuPDF and PIL."""

    def __init__(self, master, **kwargs):
        super().__init__(master, **kwargs)
        self.doc = None
        self.current_page = 0
        self.pdf_path = None

        self.grid_rowconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=0)
        self.grid_columnconfigure(0, weight=1)

        self.image_label = ctk.CTkLabel(self, text="Keine Vorschau verfügbar")
        self.image_label.grid(row=0, column=0, padx=10, pady=10, sticky="nsew")

        self.controls_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.controls_frame.grid(row=1, column=0, padx=10, pady=(0, 10), sticky="ew")
        self.controls_frame.grid_columnconfigure(1, weight=1)

        self.prev_btn = ctk.CTkButton(self.controls_frame, text="◀", width=40, command=self.prev_page)
        self.prev_btn.grid(row=0, column=0, padx=5)

        self.page_label = ctk.CTkLabel(self.controls_frame, text="Seite: - / -")
        self.page_label.grid(row=0, column=1, padx=5)

        self.next_btn = ctk.CTkButton(self.controls_frame, text="▶", width=40, command=self.next_page)
        self.next_btn.grid(row=0, column=2, padx=5)

        self.bind("<Configure>", self._on_resize)

    def _on_resize(self, event):
        if self.doc is not None and not self.doc.is_closed:
            self.after_idle(self.show_page)

    def destroy(self):
        if self.doc is not None:
            try:
                if not self.doc.is_closed:
                    self.doc.close()
            except Exception:
                pass
            self.doc = None
        super().destroy()

    def _clear_image(self):
        if hasattr(self.image_label, "_label"):
            try:
                self.image_label._label.configure(image="")
            except Exception:
                pass
        self.image_label._image = None

    def load_pdf(self, path):
        if self.doc is not None:
            try:
                if not self.doc.is_closed:
                    self.doc.close()
            except Exception:
                pass
            self.doc = None

        self.pdf_path = path
        suffix = str(path).lower().split(".")[-1]
        if suffix in ("docx", "odt", "doc", "odoc"):
            self._clear_image()
            self.image_label.configure(text=f"{suffix.upper()}-Dokument geladen.\nKeine visuelle Vorschau verfügbar.", image=None)
            self.page_label.configure(text="Seite: - / -")
            return

        try:
            with open(path, "rb") as f:
                pdf_bytes = f.read()
            self.doc = fitz.open(stream=pdf_bytes, filetype="pdf")
            self.current_page = 0
            self.show_page()
        except Exception as e:
            if self.doc is not None:
                try:
                    if not self.doc.is_closed:
                        self.doc.close()
                except Exception:
                    pass
            self.doc = None
            self.pdf_path = None
            self._clear_image()
            self.image_label.configure(text=f"Fehler beim Laden der PDF:\n{e}", image=None)
            self.page_label.configure(text="Seite: - / -")

    def show_page(self):
        if self.doc is None or self.doc.is_closed:
            return
        try:
            page = self.doc.load_page(self.current_page)
            pix = page.get_pixmap(dpi=150)
            img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)

            scale_factor = 0.8
            width = int((self.winfo_width() - 20) * scale_factor)
            height = int((self.winfo_height() - 60) * scale_factor)

            if width <= 0:
                width = int(400 * scale_factor)
            if height <= 0:
                height = int(550 * scale_factor)

            img_ratio = img.width / img.height
            container_ratio = width / height

            if img_ratio > container_ratio:
                new_width = width
                new_height = int(width / img_ratio)
            else:
                new_height = height
                new_width = int(height * img_ratio)

            img = img.resize((new_width, new_height), Image.Resampling.LANCZOS)
            ctk_img = ctk.CTkImage(light_image=img, dark_image=img, size=(new_width, new_height))

            self.image_label.configure(image=ctk_img, text="")
            self.image_label._image = ctk_img

            total_pages = len(self.doc)
            self.page_label.configure(text=f"Seite: {self.current_page + 1} / {total_pages}")
        except Exception as e:
            self._clear_image()
            self.image_label.configure(text=f"Fehler beim Rendern der Seite:\n{e}", image=None)

    def prev_page(self):
        if self.doc is not None and not self.doc.is_closed and self.current_page > 0:
            self.current_page -= 1
            self.show_page()

    def next_page(self):
        if self.doc is not None and not self.doc.is_closed and self.current_page < len(self.doc) - 1:
            self.current_page += 1
            self.show_page()
