import threading
import subprocess
import re
from pathlib import Path
import customtkinter as ctk
import tkinter as tk
from tkinter import filedialog, messagebox
from PIL import Image, ImageDraw

from core.config import AppConfig, setup_paths
from core.input_files import (
    collect_supported_input_files,
    stage_input_file,
    supported_file_dialog_patterns,
    supported_suffixes_text,
)
from core.llm import LLMClient
from core.pipeline import PipelineOrchestrator
from core.watcher import DirectoryWatcher
from core.settings import SettingsManager
from core.runtime_paths import default_token_path, default_credentials_path
from core.error_messages import friendly_error_message
from core.file_types import SUPPORTED_INPUT_SUFFIXES
from core.workflow_status import (
    WORKFLOW_EVENT_SCHEMA,
    WORKFLOW_STEPS,
    WORKFLOW_STEP_LABELS,
    WORKFLOW_STATES,
    WORKFLOW_TERMINAL_STATES,
    make_workflow_event,
    workflow_button_view,
)
from ui.pdf_preview import PDFPreviewFrame

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
except Exception:
    DND_FILES = None
    TkinterDnD = None

setup_paths()

ctk.set_appearance_mode("System")
ctk.set_default_color_theme("blue")


def split_drop_list(raw_data: str) -> list[str]:
    """Zerlegt eine tkdnd-Dateiliste ohne eigenen Tcl-Interpreter.

    tkdnd liefert Pfade als Tcl-Liste: Eintraege mit Leerzeichen stehen in
    geschweiften Klammern. Fuer das Zerlegen wurde bisher ein Wegwerf-
    ``tk.Tcl()`` erzeugt, was sporadisch mit ``TclError`` fehlschlug. Der
    damalige ``str.split()``-Fallback zerriss dann genau die Pfade, fuer die
    die Klammern gedacht sind.
    """
    parts: list[str] = []
    current: list[str] = []
    depth = 0
    for char in raw_data:
        if char == "{":
            depth += 1
            if depth == 1:
                continue
        elif char == "}":
            if depth > 0:
                depth -= 1
                if depth == 0:
                    continue
        elif char.isspace() and depth == 0:
            if current:
                parts.append("".join(current))
                current = []
            continue
        current.append(char)
    if current:
        parts.append("".join(current))
    return [part for part in parts if part.strip()]


def parse_drop_paths(raw_data: str, tk_root=None) -> list[Path]:
    if not raw_data:
        return []
    parts = None
    if tk_root is not None:
        # Ein vorhandener Interpreter kennt alle Tcl-Escapes; nur er wird
        # genutzt, kein neuer wird dafuer erzeugt.
        try:
            parts = tk_root.tk.splitlist(raw_data)
        except Exception:
            parts = None
    if parts is None:
        parts = split_drop_list(raw_data)
    return [Path(part) for part in parts if str(part).strip()]


def supported_input_suffixes_text() -> str:
    return supported_suffixes_text()


if TkinterDnD is not None:
    class DragDropCTk(ctk.CTk, TkinterDnD.DnDWrapper):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._dnd_available = False
            try:
                require_tkdnd = getattr(TkinterDnD, "require", None) or getattr(TkinterDnD, "_require")
                self.TkdndVersion = require_tkdnd(self)
                self._dnd_available = True
            except Exception:
                self.TkdndVersion = None
else:
    class DragDropCTk(ctk.CTk):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._dnd_available = False
            self.TkdndVersion = None


class App(DragDropCTk):
    def __init__(self):
        super().__init__()

        self.title("Unified OCR & LLM Pipeline")
        self.geometry("1650x820")
        self.minsize(900, 600)

        # â”€â”€ Haupt-Grid: 3 Spalten, Zeile 1 wächst â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        self.grid_columnconfigure(0, weight=3, minsize=560)
        self.grid_columnconfigure(1, weight=2, minsize=400)
        self.grid_columnconfigure(2, weight=1, minsize=250)
        self.grid_rowconfigure(1, weight=1)

        # â”€â”€ Titel â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        ctk.CTkLabel(
            self, text="Unified OCR & LLM Processing",
            font=ctk.CTkFont(size=20, weight="bold"),
        ).grid(row=0, column=0, columnspan=3, padx=20, pady=(18, 8))

        # â”€â”€ Settings laden (vor allen Widgets) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        self.settings_manager   = SettingsManager()
        self.dir_path_var       = ctk.StringVar(value=r"C:\OCR_Workdir")
        self.additional_consume_dirs = []
        self.format_var         = ctk.StringVar(value="PDF und DOCX")
        self.docx_mode_var      = ctk.StringVar(value="Lesbare DOCX")
        self.think_fusion_var   = ctk.BooleanVar(value=False)
        self.think_analysis_var = ctk.BooleanVar(value=False)
        self.organize_enabled_var = ctk.BooleanVar(value=True)
        self.confirm_sorting_each_document_var = ctk.BooleanVar(value=False)
        self.gdrive_enabled_var = ctk.BooleanVar(value=False)
        self.privacy_mode_var = ctk.StringVar(value="standard")
        self.redact_cloud_inputs_var = ctk.BooleanVar(value=False)
        self.gdrive_credentials_path_var = ctk.StringVar(value=str(default_credentials_path()))
        self.gdrive_token_path_var = ctk.StringVar(value=str(default_token_path()))
        self.gdrive_status_var = ctk.StringVar(value="Nicht verknüpft")
        self.save_docx_enabled_var = ctk.BooleanVar(value=True)
        self.save_json_enabled_var = ctk.BooleanVar(value=True)
        self.gdrive_upload_pdf_var = ctk.BooleanVar(value=True)
        self.gdrive_upload_docx_var = ctk.BooleanVar(value=False)
        self.gdrive_upload_json_var = ctk.BooleanVar(value=False)
        self.synology_enabled_var = ctk.BooleanVar(value=False)
        self.synology_base_url_var = ctk.StringVar(value="")
        self.synology_username_var = ctk.StringVar(value="")
        self.synology_password_var = ctk.StringVar(value="")
        self.synology_root_path_var = ctk.StringVar(value="")
        self.synology_upload_pdf_var = ctk.BooleanVar(value=True)
        self.synology_upload_docx_var = ctk.BooleanVar(value=False)
        self.synology_upload_json_var = ctk.BooleanVar(value=False)
        self.unload_models_enabled_var = ctk.BooleanVar(value=True)
        self.system_tray_enabled_var = ctk.BooleanVar(value=True)
        self.review_before_save_var = ctk.BooleanVar(value=False)
        self.large_pdf_reduced_var = ctk.BooleanVar(value=False)
        self.large_pdf_page_limit_var = ctk.StringVar(value="20")
        self.ocr_languages_var = ctk.StringVar(value="deu+eng")
        self.ocr_mode_var = ctk.StringVar(value="auto")
        self.force_pipeline_var = ctk.BooleanVar(value=False)
        self.debug_artifacts_enabled_var = ctk.BooleanVar(value=True)
        self.status_watcher_var = ctk.StringVar(value="Bereit")
        self.status_privacy_var = ctk.StringVar(value="standard")
        self.status_consume_var = ctk.StringVar(value="0 Dateien")
        self.status_review_var = ctk.StringVar(value="0 offen")
        self.status_paths_var = ctk.StringVar(value="0 Pfade")
        self.progress_text_var = ctk.StringVar(value="0 %")
        self.drop_status_var = ctk.StringVar(value="Dokumente ablegen")
        self.workflow_document_var = ctk.StringVar(value="Noch kein Dokument verarbeitet")
        self.workflow_detail_var = ctk.StringVar(
            value="Die Statusfelder sind grau und werden bei echten Rückmeldungen der Pipeline farbig."
        )
        self.workflow_status_buttons = {}
        self.workflow_status_events = {}
        self.workflow_active_job_id = ""
        self.workflow_active_started_at = 0.0
        self.saved_models       = {}
        self.saved_prompts      = {}
        self.prompt_version     = 1
        self.onboarding_completed = False
        self._startup_system_check_shown = False
        self.watcher = None
        self.tray_icon = None
        self._manual_processing_active = False
        self._manual_thread = None
        self._shutdown_pending = False
        self._active_processing_summary = None
        self._drag_depth = 0
        self._review_queue_window = None
        self._load_settings()
        if self.system_tray_enabled_var.get():
            self._setup_tray()

        self.protocol("WM_DELETE_WINDOW", self._on_close_window)
        self.bind("<Configure>", self._on_configure)

        # ================================================================ #
        #  LINKE SEITE - TabView mit scrollbaren Tabs                      #
        # ================================================================ #
        self.tabview = ctk.CTkTabview(self)
        self.tabview.grid(row=1, column=0, padx=(16, 8), pady=(0, 16), sticky="nsew")
        self.tabview.add("Steuerung")
        self.tabview.add("Einstellungen")

        self.tab_main     = self.tabview.tab("Steuerung")
        self.tab_settings = self.tabview.tab("Einstellungen")

        # Jeder Tab besteht nur aus einem CTkScrollableFrame der ihn füllt
        for tab in (self.tab_main, self.tab_settings):
            tab.grid_columnconfigure(0, weight=1)
            tab.grid_rowconfigure(0, weight=1)

        # Scrollbare Container für jeden Tab
        self.scroll_main = ctk.CTkScrollableFrame(self.tab_main)
        self.scroll_main.grid(row=0, column=0, sticky="nsew")
        self.scroll_main.grid_columnconfigure(0, weight=1)

        self.scroll_settings = ctk.CTkScrollableFrame(self.tab_settings)
        self.scroll_settings.grid(row=0, column=0, sticky="nsew")
        self.scroll_settings.grid_columnconfigure(0, weight=1)

        dashboard_frame = ctk.CTkFrame(self.scroll_main)
        dashboard_frame.grid(row=0, column=0, padx=12, pady=(12, 6), sticky="ew")
        for col in range(5):
            dashboard_frame.grid_columnconfigure(col, weight=1, uniform="dashboard")

        status_specs = [
            ("Status", self.status_watcher_var),
            ("Datenschutz", self.status_privacy_var),
            ("Eingang", self.status_consume_var),
            ("Review", self.status_review_var),
            ("Ablagepfade", self.status_paths_var),
        ]
        for col, (title, variable) in enumerate(status_specs):
            status_card = ctk.CTkFrame(dashboard_frame)
            status_card.grid(row=0, column=col, padx=6, pady=(10, 6), sticky="nsew")
            ctk.CTkLabel(
                status_card,
                text=title,
                text_color="gray",
                font=ctk.CTkFont(size=11),
            ).grid(row=0, column=0, padx=10, pady=(8, 0), sticky="w")
            ctk.CTkLabel(
                status_card,
                textvariable=variable,
                font=ctk.CTkFont(size=14, weight="bold"),
            ).grid(row=1, column=0, padx=10, pady=(0, 9), sticky="w")

        quick_frame = ctk.CTkFrame(dashboard_frame, fg_color="transparent")
        quick_frame.grid(row=1, column=0, columnspan=5, padx=6, pady=(0, 10), sticky="ew")
        for col in range(5):
            quick_frame.grid_columnconfigure(col, weight=1, uniform="quick")

        quick_actions = [
            ("Eingang öffnen", self._open_consume_folder),
            ("Final öffnen", self._open_final_folder),
            ("Bibliothek", self._open_document_library),
            ("Review-Queue", self._open_review_queue),
            ("Systemcheck", self._run_system_check_dialog),
        ]
        for col, (label, command) in enumerate(quick_actions):
            ctk.CTkButton(
                quick_frame,
                text=label,
                command=command,
                height=34,
            ).grid(row=0, column=col, padx=4, pady=0, sticky="ew")

        # â”€â”€ Ordner-Auswahl â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        dir_frame = ctk.CTkFrame(self.scroll_main)
        dir_frame.grid(row=1, column=0, padx=12, pady=6, sticky="ew")
        dir_frame.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(dir_frame, text="Basis-Ordner:").grid(row=0, column=0, padx=10, pady=10)
        self.dir_entry = ctk.CTkEntry(dir_frame, textvariable=self.dir_path_var)
        self.dir_entry.grid(row=0, column=1, padx=10, pady=10, sticky="ew")
        self.browse_btn = ctk.CTkButton(dir_frame, text="Ändern", command=self._browse_dir, width=90)
        self.browse_btn.grid(row=0, column=2, padx=(0, 10), pady=10)
        self.extra_inputs_btn = ctk.CTkButton(
            dir_frame,
            text="Weitere Eingaenge",
            command=self._manage_input_folders_dialog,
            width=140,
        )
        self.extra_inputs_btn.grid(row=0, column=3, padx=(0, 10), pady=10)
        ctk.CTkLabel(
            dir_frame,
            text="Ordner 'consume', 'original' und 'final' werden automatisch erstellt.",
            text_color="gray", font=ctk.CTkFont(size=11),
        ).grid(row=1, column=0, columnspan=4, padx=10, pady=(0, 8), sticky="w")

        # â”€â”€ Modell-Auswahl â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        llm_frame = ctk.CTkFrame(self.scroll_main)
        llm_frame.grid(row=2, column=0, padx=12, pady=6, sticky="ew")
        llm_frame.grid_columnconfigure(1, weight=1)

        model_rows = [
            ("GLM-OCR (Phase 2b):", "glm_ocr_var",  "glm_ocr_dropdown",  None),
            ("Vision-Modell:",      "vision_var",   "vision_dropdown",   None),
            ("Text-Fusion:",        "fusion_var",   "fusion_dropdown",   "think_fusion_var"),
            ("Analyse-Modell:",     "analysis_var", "analysis_dropdown", "think_analysis_var"),
        ]
        for i, (label, var_name, dd_name, think_var) in enumerate(model_rows):
            ctk.CTkLabel(llm_frame, text=label).grid(row=i, column=0, padx=10, pady=7, sticky="w")
            var = ctk.StringVar(value="Laden...")
            setattr(self, var_name, var)
            dd = ctk.CTkComboBox(llm_frame, variable=var, values=["Laden..."])
            dd.grid(row=i, column=1, padx=10, pady=7, sticky="ew")
            setattr(self, dd_name, dd)
            if think_var:
                ctk.CTkCheckBox(
                    llm_frame, text="Thinking",
                    variable=getattr(self, think_var), width=100,
                ).grid(row=i, column=2, padx=(0, 10), pady=7, sticky="w")

        # Neu-laden-Button in Zeile 0, Spalte 2
        ctk.CTkButton(
            llm_frame, text="Neu laden", command=self._load_models, width=90,
        ).grid(row=0, column=2, padx=(0, 10), pady=7)

        # â”€â”€ Ausgabeformat â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        fmt_frame = ctk.CTkFrame(self.scroll_main)
        fmt_frame.grid(row=3, column=0, padx=12, pady=6, sticky="ew")
        fmt_frame.grid_columnconfigure(1, weight=1)
        fmt_frame.grid_columnconfigure(3, weight=1)

        ctk.CTkLabel(fmt_frame, text="Ausgabeformat:").grid(row=0, column=0, padx=10, pady=10, sticky="w")
        ctk.CTkOptionMenu(
            fmt_frame, variable=self.format_var,
            values=["Nur PDF", "Nur TXT", "PDF und TXT", "Nur DOCX", "PDF und DOCX"],
        ).grid(row=0, column=1, padx=10, pady=10, sticky="w")
        ctk.CTkLabel(fmt_frame, text="DOCX-Modus:").grid(row=0, column=2, padx=(20, 10), pady=10, sticky="w")
        self.docx_mode_dropdown = ctk.CTkOptionMenu(
            fmt_frame, variable=self.docx_mode_var,
            values=["Lesbare DOCX", "Prüf-DOCX", "Originalgetreue DOCX"],
        )
        self.docx_mode_dropdown.grid(row=0, column=3, padx=(0, 10), pady=10, sticky="w")

        # Checkbox für Unterordnersortierung
        ctk.CTkCheckBox(
            fmt_frame, text="In Unterordner sortieren",
            variable=self.organize_enabled_var,
        ).grid(row=1, column=0, columnspan=2, padx=10, pady=(0, 10), sticky="w")

        # â”€â”€ Watchdog-Button â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        ctrl_frame = ctk.CTkFrame(self.scroll_main)
        ctrl_frame.grid(row=4, column=0, padx=12, pady=6, sticky="ew")
        ctrl_frame.grid_columnconfigure(0, weight=1)

        self.toggle_btn = ctk.CTkButton(
            ctrl_frame,
            text="Überwachung (Watchdog) starten",
            command=self._toggle_watchdog,
            height=42, font=ctk.CTkFont(size=15, weight="bold"),
        )
        self.toggle_btn.grid(row=0, column=0, padx=16, pady=14, sticky="ew")

        self.drop_frame = ctk.CTkFrame(ctrl_frame, border_width=2, border_color="#6b7280")
        self.drop_frame.grid(row=1, column=0, padx=16, pady=(0, 14), sticky="ew")
        self.drop_frame.grid_columnconfigure(0, weight=1)
        self.drop_title_label = ctk.CTkLabel(
            self.drop_frame,
            textvariable=self.drop_status_var,
            font=ctk.CTkFont(size=14, weight="bold"),
        )
        self.drop_title_label.grid(row=0, column=0, padx=14, pady=(12, 2), sticky="w")
        self.drop_hint_label = ctk.CTkLabel(
            self.drop_frame,
            text=f"PDF, Bild oder Office-Dokument | {supported_input_suffixes_text()}",
            text_color="gray",
            font=ctk.CTkFont(size=11),
        )
        self.drop_hint_label.grid(row=1, column=0, padx=14, pady=(0, 12), sticky="w")
        self.drop_select_btn = ctk.CTkButton(
            self.drop_frame,
            text="Datei auswählen",
            command=self._select_files_for_processing,
            width=130,
        )
        self.drop_select_btn.grid(row=0, column=1, rowspan=2, padx=14, pady=12, sticky="e")

        # Strukturierte Statusanzeige: Farben werden ausschließlich durch
        # bestätigte Pipeline-Ereignisse gesetzt, niemals aus Logtexten oder
        # einem geschätzten Prozentwert abgeleitet.
        workflow_frame = ctk.CTkFrame(self.scroll_main)
        workflow_frame.grid(row=5, column=0, padx=12, pady=6, sticky="ew")
        for column in range(3):
            workflow_frame.grid_columnconfigure(column, weight=1, uniform="workflow_status")
        ctk.CTkLabel(
            workflow_frame,
            text="Verarbeitungsstatus – aktuelles Dokument",
            font=ctk.CTkFont(size=14, weight="bold"),
        ).grid(row=0, column=0, columnspan=3, padx=12, pady=(10, 0), sticky="w")
        ctk.CTkLabel(
            workflow_frame,
            textvariable=self.workflow_document_var,
            text_color=("gray35", "gray70"),
            anchor="w",
        ).grid(row=1, column=0, columnspan=3, padx=12, pady=(1, 7), sticky="ew")

        for index, step in enumerate(WORKFLOW_STEPS):
            view = workflow_button_view(step, "pending")
            button = ctk.CTkButton(
                workflow_frame,
                text=view["text"],
                command=lambda current_step=step: self._show_workflow_status_detail(current_step),
                fg_color=view["color"],
                hover_color=view["hover"],
                height=48,
                font=ctk.CTkFont(size=11, weight="bold"),
            )
            button.grid(
                row=2 + index // 3,
                column=index % 3,
                padx=5,
                pady=4,
                sticky="ew",
            )
            self.workflow_status_buttons[step] = button

        ctk.CTkLabel(
            workflow_frame,
            textvariable=self.workflow_detail_var,
            justify="left",
            anchor="w",
            wraplength=650,
            text_color=("gray30", "gray75"),
        ).grid(row=5, column=0, columnspan=3, padx=12, pady=(5, 2), sticky="ew")
        ctk.CTkLabel(
            workflow_frame,
            text="Grau: ausstehend/nicht aktiv  ·  Blau: läuft  ·  Grün: bestätigt  ·  Orange: prüfen  ·  Rot: Fehler",
            justify="left",
            anchor="w",
            wraplength=650,
            text_color=("gray45", "gray60"),
            font=ctk.CTkFont(size=10),
        ).grid(row=6, column=0, columnspan=3, padx=12, pady=(0, 10), sticky="ew")

        # â”€â”€ Fortschrittsbalken â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        self.progress_var = ctk.DoubleVar(value=0.0)
        progress_frame = ctk.CTkFrame(self.scroll_main, fg_color="transparent")
        progress_frame.grid(row=6, column=0, padx=12, pady=(0, 6), sticky="ew")
        progress_frame.grid_columnconfigure(0, weight=1)
        prog_bar = ctk.CTkProgressBar(progress_frame, variable=self.progress_var)
        prog_bar.grid(row=0, column=0, padx=(0, 8), pady=0, sticky="ew")
        ctk.CTkLabel(
            progress_frame,
            textvariable=self.progress_text_var,
            width=54,
            anchor="e",
            font=ctk.CTkFont(size=12, weight="bold"),
        ).grid(row=0, column=1, padx=0, pady=0, sticky="e")

        # â”€â”€ Log-Box â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        # Feste Höhe: die Box hat ihren eigenen Scrollbalken
        self.log_box = ctk.CTkTextbox(self.scroll_main, height=260, state="disabled")
        self.log_box.grid(row=7, column=0, padx=12, pady=(0, 12), sticky="ew")

        # ================================================================ #
        #  Einstellungen-Tab                                               #
        # ================================================================ #
        prompt_defs = [
            ("Vision Prompt:",   "vision_prompt_text",   120),
            ("Fusion Prompt:",   "fusion_prompt_text",   160),
            ("Analyse Prompt:",  "analysis_prompt_text", 120),
        ]
        for i, (label, attr, height) in enumerate(prompt_defs):
            ctk.CTkLabel(self.scroll_settings, text=label).grid(
                row=i * 2, column=0, padx=10, pady=(10, 2), sticky="w"
            )
            box = ctk.CTkTextbox(self.scroll_settings, height=height)
            box.grid(row=i * 2 + 1, column=0, padx=10, pady=(0, 6), sticky="ew")
            setattr(self, attr, box)

        # â”€â”€ Ordner-Ablagepfade â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        ctk.CTkLabel(
            self.scroll_settings, text="Registrierte Ablagepfade (Person/Kategorie):",
            font=ctk.CTkFont(size=13, weight="bold")
        ).grid(row=6, column=0, padx=10, pady=(15, 2), sticky="w")

        paths_frame = ctk.CTkFrame(self.scroll_settings)
        paths_frame.grid(row=7, column=0, padx=10, pady=5, sticky="ew")
        paths_frame.grid_columnconfigure(0, weight=1)

        self.paths_box = ctk.CTkTextbox(paths_frame, height=120, state="disabled")
        self.paths_box.grid(row=0, column=0, padx=10, pady=10, sticky="ew")

        btn_frame = ctk.CTkFrame(paths_frame)
        btn_frame.grid(row=1, column=0, padx=10, pady=(0, 10), sticky="ew")
        
        ctk.CTkButton(
            btn_frame, text="Pfad hinzufügen (+)", command=self._add_path_dialog, width=150
        ).grid(row=0, column=0, padx=5, pady=5, sticky="w")
        
        ctk.CTkButton(
            btn_frame, text="Liste neu laden", command=self._reload_paths_list, width=120
        ).grid(row=0, column=1, padx=5, pady=5, sticky="w")

        ctk.CTkButton(
            btn_frame, text="Bibliothek", command=self._open_document_library, width=110
        ).grid(row=0, column=2, padx=5, pady=5, sticky="w")

        ctk.CTkButton(
            btn_frame, text="Review-Queue", command=self._open_review_queue, width=120
        ).grid(row=0, column=3, padx=5, pady=5, sticky="w")

        # â”€â”€ Dateiexport-Optionen Sektion â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        ctk.CTkLabel(
            self.scroll_settings, text="Dateiexport-Optionen:",
            font=ctk.CTkFont(size=13, weight="bold")
        ).grid(row=8, column=0, padx=10, pady=(15, 2), sticky="w")

        export_options_frame = ctk.CTkFrame(self.scroll_settings)
        export_options_frame.grid(row=9, column=0, padx=10, pady=5, sticky="ew")
        export_options_frame.grid_columnconfigure(0, weight=1)

        ctk.CTkCheckBox(
            export_options_frame, text="DOCX-Datei speichern (im Ordner 'begleitdateien')",
            variable=self.save_docx_enabled_var,
        ).grid(row=0, column=0, padx=10, pady=7, sticky="w")

        ctk.CTkCheckBox(
            export_options_frame, text="JSON-Qualitätsbericht speichern (im Ordner 'begleitdateien')",
            variable=self.save_json_enabled_var,
        ).grid(row=1, column=0, padx=10, pady=(0, 7), sticky="w")

        privacy_frame = ctk.CTkFrame(export_options_frame, fg_color="transparent")
        privacy_frame.grid(row=2, column=0, padx=10, pady=(0, 8), sticky="ew")
        privacy_frame.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(privacy_frame, text="Datenschutzmodus:").grid(row=0, column=0, padx=(0, 10), pady=4, sticky="w")
        ctk.CTkOptionMenu(
            privacy_frame,
            variable=self.privacy_mode_var,
            values=["standard", "local_only"],
        ).grid(row=0, column=1, padx=0, pady=4, sticky="w")
        ctk.CTkLabel(
            privacy_frame,
            text="local_only deaktiviert Cloud-LLMs und Google Drive für neue Jobs.",
            text_color="gray",
            font=ctk.CTkFont(size=11),
        ).grid(row=1, column=0, columnspan=2, padx=0, pady=(0, 2), sticky="w")
        ctk.CTkCheckBox(
            privacy_frame,
            text="Sensible Texte vor externen LLMs maskieren (Seitenbilder bleiben unmaskiert)",
            variable=self.redact_cloud_inputs_var,
        ).grid(row=2, column=0, columnspan=2, padx=0, pady=(4, 2), sticky="w")

        # â”€â”€ Google Drive Cloud-Upload Sektion â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        ctk.CTkLabel(
            self.scroll_settings, text="Google Drive Cloud-Upload:",
            font=ctk.CTkFont(size=13, weight="bold")
        ).grid(row=10, column=0, padx=10, pady=(15, 2), sticky="w")

        gdrive_frame = ctk.CTkFrame(self.scroll_settings)
        gdrive_frame.grid(row=11, column=0, padx=10, pady=5, sticky="ew")
        gdrive_frame.grid_columnconfigure(1, weight=1)

        # 1. Checkbox aktivieren
        ctk.CTkCheckBox(
            gdrive_frame, text="Nach Google Drive hochladen",
            variable=self.gdrive_enabled_var,
        ).grid(row=0, column=0, columnspan=3, padx=10, pady=10, sticky="w")

        # GDrive Upload Dateitypen-Auswahl
        gdrive_types_frame = ctk.CTkFrame(gdrive_frame, fg_color="transparent")
        gdrive_types_frame.grid(row=1, column=0, columnspan=3, padx=10, pady=(0, 10), sticky="w")
        
        ctk.CTkCheckBox(
            gdrive_types_frame, text="PDF hochladen",
            variable=self.gdrive_upload_pdf_var,
        ).pack(side="left", padx=(0, 15))

        ctk.CTkCheckBox(
            gdrive_types_frame, text="DOCX hochladen",
            variable=self.gdrive_upload_docx_var,
        ).pack(side="left", padx=(0, 15))

        ctk.CTkCheckBox(
            gdrive_types_frame, text="JSON hochladen",
            variable=self.gdrive_upload_json_var,
        ).pack(side="left")

        # 2. Credentials-Dateipfad Auswählen
        ctk.CTkLabel(gdrive_frame, text="credentials.json Pfad:").grid(row=2, column=0, padx=10, pady=5, sticky="w")
        self.gdrive_cred_entry = ctk.CTkEntry(gdrive_frame, textvariable=self.gdrive_credentials_path_var)
        self.gdrive_cred_entry.grid(row=2, column=1, padx=10, pady=5, sticky="ew")
        
        def browse_credentials():
            path = filedialog.askopenfilename(
                initialdir=".",
                title="Wähle Google credentials.json",
                filetypes=[("JSON Files", "*.json")]
            )
            if path:
                self.gdrive_credentials_path_var.set(path)
                
        self.gdrive_cred_browse_btn = ctk.CTkButton(gdrive_frame, text="Durchsuchen", command=browse_credentials, width=90)
        self.gdrive_cred_browse_btn.grid(row=2, column=2, padx=(0, 10), pady=5)

        # 3. Status Anzeige
        status_frame = ctk.CTkFrame(gdrive_frame, fg_color="transparent")
        status_frame.grid(row=3, column=0, columnspan=3, padx=10, pady=10, sticky="ew")
        
        ctk.CTkLabel(status_frame, text="Status:").pack(side="left", padx=(0, 5))
        self.status_lbl = ctk.CTkLabel(
            status_frame, textvariable=self.gdrive_status_var, 
            text_color="#1f6aa5", font=ctk.CTkFont(weight="bold")
        )
        self.status_lbl.pack(side="left")

        # 4. Buttons für Verknüpfen / Trennen
        btn_gdrive_frame = ctk.CTkFrame(gdrive_frame, fg_color="transparent")
        btn_gdrive_frame.grid(row=4, column=0, columnspan=3, padx=10, pady=(0, 10), sticky="ew")

        def start_link_flow():
            import os
            cred_path = self.gdrive_credentials_path_var.get()
            tok_path = self.gdrive_token_path_var.get()
            if not os.path.exists(cred_path):
                messagebox.showerror(
                    "Fehler", 
                    f"Die Datei 'credentials.json' konnte unter dem Pfad '{cred_path}' nicht gefunden werden.\n"
                    f"Bitte laden Sie die OAuth-Client-ID-Credentials in der Google Cloud Console herunter."
                )
                return

            self.gdrive_status_var.set("Warte auf Browser-Login...")
            self.gdrive_link_btn.configure(state="disabled")
            
            def auth_thread():
                from core.cloud.gdrive_client import GoogleDriveClient
                client = GoogleDriveClient()
                try:
                    email = client.authenticate(cred_path, tok_path)
                    self.after(0, lambda: messagebox.showinfo("Erfolg", f"Google Drive erfolgreich verknüpft mit:\n{email}"))
                except Exception as ex:
                    self.after(0, lambda: messagebox.showerror("Fehler", f"Verbindung fehlgeschlagen:\n{ex}"))
                finally:
                    self.after(0, self._update_gdrive_status)
                    self.after(0, lambda: self.gdrive_link_btn.configure(state="normal"))

            threading.Thread(target=auth_thread, daemon=True).start()

        def disconnect_gdrive():
            tok_path = self.gdrive_token_path_var.get()
            from core.cloud.gdrive_client import GoogleDriveClient
            client = GoogleDriveClient()
            try:
                client.logout(tok_path)
                messagebox.showinfo("Verbindung getrennt", "Die Verknüpfung zu Google Drive wurde aufgehoben.")
            except Exception as ex:
                messagebox.showerror("Fehler", f"Konnte Verbindung nicht trennen:\n{ex}")
            finally:
                self._update_gdrive_status()

        self.gdrive_link_btn = ctk.CTkButton(
            btn_gdrive_frame, text="Google Drive verknüpfen", command=start_link_flow, width=170
        )
        self.gdrive_link_btn.grid(row=0, column=0, padx=5, pady=5)

        self.gdrive_unlink_btn = ctk.CTkButton(
            btn_gdrive_frame, text="Verbindung trennen", command=disconnect_gdrive, width=150,
            fg_color="#c93434", hover_color="#9e2a2a"
        )
        self.gdrive_unlink_btn.grid(row=0, column=1, padx=5, pady=5)

        ctk.CTkLabel(
            self.scroll_settings, text="Synology / WebDAV Upload:",
            font=ctk.CTkFont(size=13, weight="bold")
        ).grid(row=12, column=0, padx=10, pady=(15, 2), sticky="w")

        synology_frame = ctk.CTkFrame(self.scroll_settings)
        synology_frame.grid(row=13, column=0, padx=10, pady=5, sticky="ew")
        synology_frame.grid_columnconfigure(1, weight=1)

        ctk.CTkCheckBox(
            synology_frame,
            text="Nach Synology/WebDAV hochladen",
            variable=self.synology_enabled_var,
        ).grid(row=0, column=0, columnspan=3, padx=10, pady=10, sticky="w")

        synology_types_frame = ctk.CTkFrame(synology_frame, fg_color="transparent")
        synology_types_frame.grid(row=1, column=0, columnspan=3, padx=10, pady=(0, 10), sticky="w")
        ctk.CTkCheckBox(
            synology_types_frame, text="PDF hochladen",
            variable=self.synology_upload_pdf_var,
        ).pack(side="left", padx=(0, 15))
        ctk.CTkCheckBox(
            synology_types_frame, text="DOCX hochladen",
            variable=self.synology_upload_docx_var,
        ).pack(side="left", padx=(0, 15))
        ctk.CTkCheckBox(
            synology_types_frame, text="JSON hochladen",
            variable=self.synology_upload_json_var,
        ).pack(side="left")

        ctk.CTkLabel(synology_frame, text="WebDAV-URL:").grid(row=2, column=0, padx=10, pady=5, sticky="w")
        ctk.CTkEntry(
            synology_frame,
            textvariable=self.synology_base_url_var,
            placeholder_text="https://dein-nas:5006",
        ).grid(row=2, column=1, columnspan=2, padx=10, pady=5, sticky="ew")

        ctk.CTkLabel(synology_frame, text="Zielwurzel:").grid(row=3, column=0, padx=10, pady=5, sticky="w")
        ctk.CTkEntry(
            synology_frame,
            textvariable=self.synology_root_path_var,
            placeholder_text="optional, z. B. OCR",
        ).grid(row=3, column=1, columnspan=2, padx=10, pady=5, sticky="ew")

        ctk.CTkLabel(synology_frame, text="Benutzer:").grid(row=4, column=0, padx=10, pady=5, sticky="w")
        ctk.CTkEntry(synology_frame, textvariable=self.synology_username_var).grid(
            row=4, column=1, columnspan=2, padx=10, pady=5, sticky="ew"
        )

        ctk.CTkLabel(synology_frame, text="Passwort:").grid(row=5, column=0, padx=10, pady=5, sticky="w")
        ctk.CTkEntry(synology_frame, textvariable=self.synology_password_var, show="*").grid(
            row=5, column=1, columnspan=2, padx=10, pady=5, sticky="ew"
        )

        ctk.CTkLabel(
            synology_frame,
            text="Empfohlen: HTTPS/WebDAV auf Port 5006 und ein eigener NAS-Benutzer mit Schreibrechten nur im Zielordner.",
            text_color="gray",
            font=ctk.CTkFont(size=11),
            wraplength=650,
        ).grid(row=6, column=0, columnspan=3, padx=10, pady=(0, 10), sticky="w")

        ctk.CTkButton(
            synology_frame,
            text="Verbindung testen",
            command=self._test_synology_connection,
            width=160,
        ).grid(row=7, column=0, padx=10, pady=(0, 10), sticky="w")

        # OCR und große PDFs
        ctk.CTkLabel(
            self.scroll_settings, text="OCR & große PDFs:",
            font=ctk.CTkFont(size=13, weight="bold")
        ).grid(row=14, column=0, padx=10, pady=(15, 2), sticky="w")

        large_pdf_frame = ctk.CTkFrame(self.scroll_settings)
        large_pdf_frame.grid(row=15, column=0, padx=10, pady=5, sticky="ew")
        large_pdf_frame.grid_columnconfigure(0, weight=1)

        ocr_options_frame = ctk.CTkFrame(large_pdf_frame, fg_color="transparent")
        ocr_options_frame.grid(row=0, column=0, padx=10, pady=(8, 4), sticky="ew")
        ocr_options_frame.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(ocr_options_frame, text="OCR-Sprachen:").grid(
            row=0, column=0, padx=(0, 8), pady=4, sticky="w"
        )
        ctk.CTkEntry(
            ocr_options_frame,
            textvariable=self.ocr_languages_var,
            placeholder_text="deu+eng",
        ).grid(row=0, column=1, padx=(0, 12), pady=4, sticky="ew")
        ctk.CTkLabel(ocr_options_frame, text="Modus:").grid(
            row=0, column=2, padx=(0, 8), pady=4, sticky="w"
        )
        ctk.CTkOptionMenu(
            ocr_options_frame,
            variable=self.ocr_mode_var,
            values=["auto", "redo", "force"],
            width=110,
        ).grid(row=0, column=3, pady=4, sticky="e")
        ctk.CTkLabel(
            large_pdf_frame,
            text="Mehrere Sprachpakete mit '+' angeben. 'auto' bewahrt vorhandenen Digitaltext; 'redo' und 'force' sind Reparaturmodi.",
            text_color="gray",
            wraplength=650,
        ).grid(row=1, column=0, padx=10, pady=(0, 8), sticky="w")

        ctk.CTkCheckBox(
            large_pdf_frame, text="Schnellmodus ab der konfigurierten Seitengrenze aktivieren",
            variable=self.large_pdf_reduced_var,
        ).grid(row=2, column=0, padx=10, pady=7, sticky="w")

        # â”€â”€ System-Optionen Sektion â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        limit_frame = ctk.CTkFrame(large_pdf_frame, fg_color="transparent")
        limit_frame.grid(row=3, column=0, padx=10, pady=(0, 10), sticky="w")
        ctk.CTkLabel(limit_frame, text="Seitengrenze:").grid(row=0, column=0, padx=(0, 8), pady=0, sticky="w")
        ctk.CTkEntry(limit_frame, textvariable=self.large_pdf_page_limit_var, width=72).grid(row=0, column=1, padx=(0, 8), pady=0)
        ctk.CTkLabel(limit_frame, text="1-1000 Seiten", text_color="gray").grid(row=0, column=2, pady=0, sticky="w")
        ctk.CTkLabel(
            large_pdf_frame,
            text="Hinweis: Der Schnellmodus überspringt Detailanalyse und Qualitätsprüfung. Für Archivqualität deaktiviert lassen.",
            text_color="gray",
            wraplength=650,
        ).grid(row=4, column=0, padx=10, pady=(0, 10), sticky="w")

        ctk.CTkLabel(
            self.scroll_settings, text="System-Optionen:",
            font=ctk.CTkFont(size=13, weight="bold")
        ).grid(row=16, column=0, padx=10, pady=(15, 2), sticky="w")

        system_options_frame = ctk.CTkFrame(self.scroll_settings)
        system_options_frame.grid(row=17, column=0, padx=10, pady=5, sticky="ew")
        system_options_frame.grid_columnconfigure(0, weight=1)

        ctk.CTkCheckBox(
            system_options_frame, text="Modelle nach Aufruf entladen (keep_alive = 0)",
            variable=self.unload_models_enabled_var,
        ).grid(row=0, column=0, padx=10, pady=7, sticky="w")

        ctk.CTkCheckBox(
            system_options_frame, text="In System-Tray minimieren bei Schließen/Minimieren",
            variable=self.system_tray_enabled_var,
        ).grid(row=1, column=0, padx=10, pady=(0, 7), sticky="w")

        ctk.CTkCheckBox(
            system_options_frame, text="Vor dem Speichern prüfen (Manueller Review)",
            variable=self.review_before_save_var,
        ).grid(row=2, column=0, padx=10, pady=(0, 7), sticky="w")

        ctk.CTkCheckBox(
            system_options_frame, text="Bei jeder Einsortierung Zielpfad bestaetigen",
            variable=self.confirm_sorting_each_document_var,
        ).grid(row=3, column=0, padx=10, pady=(0, 7), sticky="w")

        ctk.CTkCheckBox(
            system_options_frame, text="Pipeline erzwingen (Cache ignorieren)",
            variable=self.force_pipeline_var,
        ).grid(row=4, column=0, padx=10, pady=(0, 7), sticky="w")

        ctk.CTkCheckBox(
            system_options_frame, text="Debug-/Diagnoseberichte speichern",
            variable=self.debug_artifacts_enabled_var,
        ).grid(row=5, column=0, padx=10, pady=(0, 7), sticky="w")

        ctk.CTkButton(
            system_options_frame,
            text="Systemcheck ausführen",
            command=self._run_system_check_dialog,
            width=190,
        ).grid(row=6, column=0, padx=10, pady=(4, 10), sticky="w")

        # â”€â”€ API & Provider Einstellungen â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        ctk.CTkButton(
            system_options_frame,
            text="Laufzeitdaten bereinigen...",
            command=self._open_cleanup_dialog,
            width=190,
        ).grid(row=7, column=0, padx=10, pady=(0, 10), sticky="w")

        ctk.CTkLabel(
            self.scroll_settings, text="API & Provider Einstellungen:",
            font=ctk.CTkFont(size=13, weight="bold")
        ).grid(row=18, column=0, padx=10, pady=(15, 2), sticky="w")

        api_frame = ctk.CTkFrame(self.scroll_settings)
        api_frame.grid(row=19, column=0, padx=10, pady=5, sticky="ew")
        api_frame.grid_columnconfigure(0, weight=1)
        
        ctk.CTkButton(
            api_frame, text="API-Schlüssel verwalten...",
            command=self._open_api_settings_dialog, width=250
        ).grid(row=0, column=0, padx=10, pady=10, sticky="w")

        # Speicher-Button nach unten verschoben
        ctk.CTkButton(
            self.scroll_settings, text="Einstellungen speichern",
            command=self._save_settings_clicked,
        ).grid(row=20, column=0, padx=10, pady=18)

        # Prompts befüllen
        defaults = self.settings_manager.default_prompts
        self.vision_prompt_text.insert("1.0",   self.saved_prompts.get("vision",   defaults["vision"]))
        self.fusion_prompt_text.insert("1.0",   self.saved_prompts.get("fusion",   defaults["fusion"]))
        self.analysis_prompt_text.insert("1.0", self.saved_prompts.get("analysis", defaults["analysis"]))

        # ================================================================ #
        #  RECHTE SEITE - Streaming-Panel mit scrollbarem Wrapper          #
        # ================================================================ #
        # Äußerer Rahmen (bleibt fest im Haupt-Grid)
        right_outer = ctk.CTkFrame(self)
        right_outer.grid(row=1, column=1, padx=(8, 16), pady=(0, 16), sticky="nsew")
        right_outer.grid_columnconfigure(0, weight=1)
        right_outer.grid_rowconfigure(1, weight=1)   # scroll_stream wächst

        # Überschrift (außerhalb des Scrollbereichs - immer sichtbar)
        ctk.CTkLabel(
            right_outer, text="LLM Live-Ausgabe",
            font=ctk.CTkFont(size=15, weight="bold"),
        ).grid(row=0, column=0, padx=14, pady=(14, 4), sticky="w")

        # Scrollbarer Container für Thinking + Output
        stream_scroll = ctk.CTkScrollableFrame(right_outer)
        stream_scroll.grid(row=1, column=0, padx=6, pady=(0, 10), sticky="nsew")
        stream_scroll.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            stream_scroll, text="Denkprozess (Chain of Thought):",
            font=ctk.CTkFont(size=13, weight="bold"),
        ).grid(row=0, column=0, padx=10, pady=(10, 4), sticky="w")

        self.thinking_box = ctk.CTkTextbox(
            stream_scroll, height=220,
            fg_color="#2E2E2E", text_color="#E0E0E0",
            font=ctk.CTkFont(family="Courier New", size=12),
            corner_radius=8, border_width=1, border_color="#505050",
            state="disabled",
        )
        self.thinking_box.grid(row=1, column=0, padx=10, pady=(0, 14), sticky="ew")

        ctk.CTkLabel(
            stream_scroll, text="Generierte Ausgabe:",
            font=ctk.CTkFont(size=13, weight="bold"),
        ).grid(row=2, column=0, padx=10, pady=(4, 4), sticky="w")

        self.output_box = ctk.CTkTextbox(
            stream_scroll, height=320,
            font=ctk.CTkFont(size=12),
            corner_radius=8,
            state="disabled",
        )
        self.output_box.grid(row=3, column=0, padx=10, pady=(0, 14), sticky="ew")

        # ================================================================ #
        #  RECHTE SEITE 2 - PDF-Vorschau Panel                             #
        # ================================================================ #
        preview_outer = ctk.CTkFrame(self)
        preview_outer.grid(row=1, column=2, padx=(8, 16), pady=(0, 16), sticky="nsew")
        preview_outer.grid_columnconfigure(0, weight=1)
        preview_outer.grid_rowconfigure(1, weight=1)

        ctk.CTkLabel(
            preview_outer, text="PDF-Vorschau",
            font=ctk.CTkFont(size=15, weight="bold"),
        ).grid(row=0, column=0, padx=14, pady=(14, 4), sticky="w")

        self.pdf_preview_panel = PDFPreviewFrame(preview_outer)
        self.pdf_preview_panel.grid(row=1, column=0, padx=10, pady=(0, 10), sticky="nsew")

        self.watcher = None
        self._setup_drag_and_drop()
        self.after(120, self._load_models)
        self.after(150, self._reload_paths_list)
        self.after(250, self._refresh_dashboard_periodically)
        self.after(320, self._show_startup_system_check_if_needed)
        self.after(520, self._show_onboarding_if_needed)
        self.privacy_mode_var.trace_add("write", lambda *_: self._refresh_dashboard())
        self.redact_cloud_inputs_var.trace_add("write", lambda *_: self._refresh_dashboard())

    # ------------------------------------------------------------------ #
    #  Settings laden / speichern                                          #
    # ------------------------------------------------------------------ #

    def _load_settings(self):
        s = self.settings_manager.settings
        self.dir_path_var.set(s.get("base_dir", r"C:\OCR_Workdir"))
        self.additional_consume_dirs = list(s.get("additional_consume_dirs", []))
        self.format_var.set(s.get("output_format", "PDF und DOCX"))
        self.docx_mode_var.set(s.get("docx_mode", "Lesbare DOCX"))
        self.think_fusion_var.set(s.get("think_fusion", False))
        self.think_analysis_var.set(s.get("think_analysis", False))
        self.organize_enabled_var.set(s.get("organize_enabled", True))
        self.gdrive_enabled_var.set(s.get("gdrive_enabled", False))
        self.privacy_mode_var.set(s.get("privacy_mode", "standard"))
        self.redact_cloud_inputs_var.set(s.get("redact_cloud_inputs", False))
        self.gdrive_credentials_path_var.set(s.get("gdrive_credentials_path", "credentials.json"))
        self.gdrive_token_path_var.set(s.get("gdrive_token_path", str(default_token_path())))
        self.save_docx_enabled_var.set(s.get("save_docx_enabled", True))
        self.save_json_enabled_var.set(s.get("save_json_enabled", True))
        self.gdrive_upload_pdf_var.set(s.get("gdrive_upload_pdf", True))
        self.gdrive_upload_docx_var.set(s.get("gdrive_upload_docx", False))
        self.gdrive_upload_json_var.set(s.get("gdrive_upload_json", False))
        self.synology_enabled_var.set(s.get("synology_enabled", False))
        self.synology_base_url_var.set(s.get("synology_base_url", ""))
        self.synology_username_var.set(s.get("synology_username", ""))
        self.synology_password_var.set(s.get("synology_password", ""))
        self.synology_root_path_var.set(s.get("synology_root_path", ""))
        self.synology_upload_pdf_var.set(s.get("synology_upload_pdf", True))
        self.synology_upload_docx_var.set(s.get("synology_upload_docx", False))
        self.synology_upload_json_var.set(s.get("synology_upload_json", False))
        self.unload_models_enabled_var.set(s.get("unload_models_enabled", True))
        self.system_tray_enabled_var.set(s.get("system_tray_enabled", True))
        self.review_before_save_var.set(s.get("review_before_save", False))
        self.confirm_sorting_each_document_var.set(s.get("confirm_sorting_each_document", False))
        self.large_pdf_reduced_var.set(s.get("large_pdf_reduced", False))
        self.large_pdf_page_limit_var.set(str(s.get("large_pdf_page_limit", 20)))
        self.ocr_languages_var.set(s.get("ocr_languages", "deu+eng"))
        self.ocr_mode_var.set(s.get("ocr_mode", "auto"))
        self.force_pipeline_var.set(s.get("force_pipeline", False))
        self.debug_artifacts_enabled_var.set(s.get("debug_artifacts_enabled", True))
        self.onboarding_completed = s.get("onboarding_completed", False)
        self.prompt_version = s.get("prompt_version", 1)
        self.saved_models = s.get("models", {})
        self.saved_prompts = s.get("prompts", {})
        self._update_gdrive_status()

    def _update_gdrive_status(self):
        import os
        from core.cloud.gdrive_client import GoogleDriveClient
        client = GoogleDriveClient()
        token_path = self.gdrive_token_path_var.get()
        if client.is_authenticated(token_path):
            email = client.get_authenticated_user_email(token_path)
            if email:
                self.gdrive_status_var.set(f"Verknüpft als: {email}")
                if hasattr(self, 'gdrive_link_btn'):
                    self.gdrive_link_btn.configure(text="Erneut verknüpfen")
                return
        self.gdrive_status_var.set("Nicht verknüpft")
        if hasattr(self, 'gdrive_link_btn'):
            self.gdrive_link_btn.configure(text="Google Drive verknüpfen")

    def _test_synology_connection(self):
        try:
            from core.cloud.synology_client import SynologyWebDAVClient

            client = SynologyWebDAVClient(
                base_url=self.synology_base_url_var.get(),
                username=self.synology_username_var.get(),
                password=self.synology_password_var.get(),
                root_path=self.synology_root_path_var.get(),
            )
            if client.test_connection():
                messagebox.showinfo("Synology/WebDAV", "Verbindung erfolgreich.")
            else:
                messagebox.showwarning("Synology/WebDAV", "Verbindung fehlgeschlagen oder unvollständig konfiguriert.")
        except Exception as exc:
            messagebox.showerror("Synology/WebDAV", f"Verbindungstest fehlgeschlagen:\n{exc}")

    def _app_config(self) -> AppConfig:
        return AppConfig(
            self.dir_path_var.get(),
            self.additional_consume_dirs,
            large_pdf_page_limit=self._large_pdf_page_limit_value(),
        )

    def _large_pdf_page_limit_value(self) -> int:
        raw_value = str(self.large_pdf_page_limit_var.get() or "").strip()
        try:
            value = int(raw_value)
        except ValueError as exc:
            raise ValueError("Die Seitengrenze fuer grosse PDFs muss eine ganze Zahl sein.") from exc
        if not 1 <= value <= 1000:
            raise ValueError("Die Seitengrenze fuer grosse PDFs muss zwischen 1 und 1000 liegen.")
        return value

    def _manage_input_folders_dialog(self):
        base_dir = self.dir_path_var.get()
        if not base_dir:
            messagebox.showerror("Fehler", "Bitte waehlen Sie zuerst einen Basis-Ordner aus.")
            return

        dialog = ctk.CTkToplevel(self)
        dialog.title("Weitere Eingaenge")
        dialog.geometry("760x430")
        dialog.transient(self)
        dialog.grab_set()
        dialog.grid_columnconfigure(0, weight=1)
        dialog.grid_rowconfigure(2, weight=1)

        primary = AppConfig(base_dir).consume_dir
        ctk.CTkLabel(
            dialog,
            text=f"Primaerer Eingang: {primary}",
            anchor="w",
        ).grid(row=0, column=0, padx=18, pady=(16, 4), sticky="ew")
        ctk.CTkLabel(
            dialog,
            text="Zusaetzliche Ordner werden vom Watchdog ebenfalls verarbeitet.",
            text_color="gray",
            anchor="w",
        ).grid(row=1, column=0, padx=18, pady=(0, 8), sticky="ew")

        listbox = tk.Listbox(dialog, height=10, activestyle="dotbox")
        listbox.grid(row=2, column=0, padx=18, pady=8, sticky="nsew")
        for path in self.additional_consume_dirs:
            listbox.insert("end", path)

        button_frame = ctk.CTkFrame(dialog, fg_color="transparent")
        button_frame.grid(row=3, column=0, padx=18, pady=(8, 16), sticky="ew")
        for col in range(4):
            button_frame.grid_columnconfigure(col, weight=1)

        def add_folder():
            initial = self.dir_path_var.get() or str(primary)
            path = filedialog.askdirectory(initialdir=initial, parent=dialog)
            if not path:
                return
            existing = {listbox.get(i).lower() for i in range(listbox.size())}
            if path.lower() not in existing:
                listbox.insert("end", path)

        def remove_selected():
            selected = list(listbox.curselection())
            for index in reversed(selected):
                listbox.delete(index)

        def save_and_close():
            self.additional_consume_dirs = [listbox.get(i) for i in range(listbox.size())]
            if not self._save_settings(show_message=False):
                return
            self._refresh_dashboard()
            if self.watcher and self.watcher.is_running:
                messagebox.showinfo(
                    "Weitere Eingaenge",
                    "Die Aenderung wird beim naechsten Start der Ueberwachung aktiv.",
                )
            dialog.destroy()

        ctk.CTkButton(button_frame, text="Hinzufuegen", command=add_folder).grid(row=0, column=0, padx=(0, 6), sticky="ew")
        ctk.CTkButton(button_frame, text="Entfernen", command=remove_selected).grid(row=0, column=1, padx=6, sticky="ew")
        ctk.CTkButton(button_frame, text="Speichern", command=save_and_close).grid(row=0, column=2, padx=6, sticky="ew")
        ctk.CTkButton(button_frame, text="Schliessen", command=dialog.destroy, fg_color="#6b7280").grid(row=0, column=3, padx=(6, 0), sticky="ew")

    def _open_folder(self, path):
        import os
        from pathlib import Path

        try:
            folder = Path(path)
            folder.mkdir(parents=True, exist_ok=True)
            os.startfile(str(folder))
        except Exception as exc:
            messagebox.showerror("Fehler", f"Ordner konnte nicht geöffnet werden:\n{exc}")

    def _open_consume_folder(self):
        base_dir = self.dir_path_var.get()
        if not base_dir:
            messagebox.showerror("Fehler", "Bitte wählen Sie zuerst einen Basis-Ordner aus.")
            return
        self._open_folder(self._app_config().consume_dir)

    def _open_final_folder(self):
        base_dir = self.dir_path_var.get()
        if not base_dir:
            messagebox.showerror("Fehler", "Bitte wählen Sie zuerst einen Basis-Ordner aus.")
            return
        self._open_folder(self._app_config().final_dir)

    def _refresh_dashboard(self):
        base_dir = self.dir_path_var.get()
        if not base_dir:
            self.status_watcher_var.set("Bereit")
            self.status_privacy_var.set(self.privacy_mode_var.get() or "standard")
            self.status_consume_var.set("-")
            self.status_review_var.set("-")
            self.status_paths_var.set("-")
            return

        try:
            config = self._app_config()
            consume_count = 0
            active_dirs = 0
            for consume_dir in config.consume_dirs:
                if consume_dir.exists():
                    active_dirs += 1
                    consume_count += sum(
                        1
                        for item in consume_dir.iterdir()
                        if item.is_file() and item.suffix.lower() in SUPPORTED_INPUT_SUFFIXES
                    )
            suffix = f" / {active_dirs} Eing." if active_dirs > 1 else ""
            self.status_consume_var.set(f"{consume_count} Dateien{suffix}")
        except Exception:
            self.status_consume_var.set("-")

        if self.watcher and self.watcher.is_running:
            self.status_watcher_var.set("Aktiv")
        else:
            self.status_watcher_var.set("Bereit")

        active_summary = self._active_processing_summary if self.watcher and self.watcher.is_running else None
        privacy = (active_summary or {}).get("privacy_mode") or self.privacy_mode_var.get() or "standard"
        redact_cloud_inputs = (
            (active_summary or {}).get("redact_cloud_inputs")
            if active_summary is not None
            else self.redact_cloud_inputs_var.get()
        )
        if redact_cloud_inputs and privacy != "local_only":
            privacy = f"{privacy} + Textmaskierung (Bilder unmaskiert)"
        self.status_privacy_var.set(privacy)

        try:
            from core.cloud.folder_registry import FolderRegistry

            registry = FolderRegistry(base_dir)
            self.status_paths_var.set(f"{len(registry.get_known_paths())} Pfade")
        except Exception:
            self.status_paths_var.set("-")

        try:
            from core.local_store import LocalStore
            from pathlib import Path

            if not (Path(base_dir) / "unified_ocr.sqlite3").exists():
                self.status_review_var.set("0 offen")
                return
            store = LocalStore(base_dir)
            pending_count = len(store.list_recoverable_work(limit=200))
            self.status_review_var.set(f"{pending_count} offen")
        except Exception:
            self.status_review_var.set("-")

    def _refresh_dashboard_periodically(self):
        self._refresh_dashboard()
        self.after(5000, self._refresh_dashboard_periodically)

    def _browse_dir(self):
        path = filedialog.askdirectory(initialdir=self.dir_path_var.get())
        if path:
            self.dir_path_var.set(path)
            self._save_settings(show_message=False)
            self._reload_paths_list()
            self._refresh_dashboard()

    def _save_settings_clicked(self):
        self._save_settings(show_message=True)
        self._reload_paths_list()
        self._refresh_dashboard()

    def _save_settings(self, show_message: bool = False):
        try:
            large_pdf_page_limit = self._large_pdf_page_limit_value()
        except Exception as e:
            messagebox.showerror("Fehler", friendly_error_message(e, context="Konnte Einstellungen nicht speichern."))
            return False

        settings = {
            "base_dir":       self.dir_path_var.get(),
            "additional_consume_dirs": self.additional_consume_dirs,
            "output_format":  self.format_var.get(),
            "docx_mode":      self.docx_mode_var.get(),
            "think_fusion":   self.think_fusion_var.get(),
            "think_analysis": self.think_analysis_var.get(),
            "organize_enabled": self.organize_enabled_var.get(),
            "gdrive_enabled": self.gdrive_enabled_var.get(),
            "privacy_mode": self.privacy_mode_var.get(),
            "redact_cloud_inputs": self.redact_cloud_inputs_var.get(),
            "gdrive_credentials_path": self.gdrive_credentials_path_var.get(),
            "gdrive_token_path": self.gdrive_token_path_var.get(),
            "save_docx_enabled": self.save_docx_enabled_var.get(),
            "save_json_enabled": self.save_json_enabled_var.get(),
            "gdrive_upload_pdf": self.gdrive_upload_pdf_var.get(),
            "gdrive_upload_docx": self.gdrive_upload_docx_var.get(),
            "gdrive_upload_json": self.gdrive_upload_json_var.get(),
            "synology_enabled": self.synology_enabled_var.get(),
            "synology_base_url": self.synology_base_url_var.get(),
            "synology_username": self.synology_username_var.get(),
            "synology_password": self.synology_password_var.get(),
            "synology_root_path": self.synology_root_path_var.get(),
            "synology_upload_pdf": self.synology_upload_pdf_var.get(),
            "synology_upload_docx": self.synology_upload_docx_var.get(),
            "synology_upload_json": self.synology_upload_json_var.get(),
            "unload_models_enabled": self.unload_models_enabled_var.get(),
            "system_tray_enabled": self.system_tray_enabled_var.get(),
            "review_before_save": self.review_before_save_var.get(),
            "confirm_sorting_each_document": self.confirm_sorting_each_document_var.get(),
            "large_pdf_reduced": self.large_pdf_reduced_var.get(),
            "large_pdf_page_limit": large_pdf_page_limit,
            "ocr_languages": self.ocr_languages_var.get(),
            "ocr_mode": self.ocr_mode_var.get(),
            "force_pipeline": self.force_pipeline_var.get(),
            "debug_artifacts_enabled": self.debug_artifacts_enabled_var.get(),
            "onboarding_completed": self.onboarding_completed,
            "prompt_version": self.prompt_version,
            "models": {
                "vision":   self.vision_var.get(),
                "fusion":   self.fusion_var.get(),
                "analysis": self.analysis_var.get(),
                "glm_ocr":  self.glm_ocr_var.get(),
            },
            "prompts": {
                "vision":   self.vision_prompt_text.get("1.0", "end-1c").strip(),
                "fusion":   self.fusion_prompt_text.get("1.0", "end-1c").strip(),
                "analysis": self.analysis_prompt_text.get("1.0", "end-1c").strip(),
                "image_description": self.saved_prompts.get(
                    "image_description",
                    self.settings_manager.default_prompts["image_description"],
                ),
            },
        }
        try:
            self.settings_manager.save(settings)
            self.saved_models  = settings["models"]
            self.saved_prompts = settings["prompts"]

            # Tray-Verhalten anpassen
            now_tray_enabled = self.system_tray_enabled_var.get()
            if now_tray_enabled and not self.tray_icon:
                self._setup_tray()
            elif not now_tray_enabled and self.tray_icon:
                self.tray_icon.stop()
                self.tray_icon = None

            if show_message:
                suffix = (
                    " Sie gelten für neue Jobs nach dem nächsten Start der Überwachung."
                    if self.watcher and self.watcher.is_running
                    else ""
                )
                messagebox.showinfo("Gespeichert", f"Einstellungen wurden gespeichert.{suffix}")
            self._refresh_dashboard()
            return True
        except Exception as e:
            messagebox.showerror("Fehler", friendly_error_message(e, context="Konnte Einstellungen nicht speichern."))
            return False

    def _run_system_check(self) -> dict:
        from core.system_check import run_system_check

        return run_system_check(
            self.dir_path_var.get(),
            credentials_path=self.gdrive_credentials_path_var.get(),
            token_path=self.gdrive_token_path_var.get(),
            ocr_languages=self.ocr_languages_var.get(),
        )

    def _show_startup_system_check_if_needed(self):
        if self._startup_system_check_shown:
            return
        self._startup_system_check_shown = True
        try:
            checks = self._run_system_check()
        except Exception as exc:
            self._log(f"Systemcheck beim Start fehlgeschlagen: {exc}")
            return
        if not checks.get("ok"):
            self._run_system_check_dialog(checks=checks, startup=True)

    def _run_system_check_dialog(self, *, checks: dict | None = None, startup: bool = False):
        from core.system_check import format_system_check, run_system_check

        if checks is None:
            checks = run_system_check(
                self.dir_path_var.get(),
                credentials_path=self.gdrive_credentials_path_var.get(),
                token_path=self.gdrive_token_path_var.get(),
                ocr_languages=self.ocr_languages_var.get(),
            )
        dialog = ctk.CTkToplevel(self)
        dialog.title("Systemcheck" if not startup else "Systemcheck beim Start")
        dialog.geometry("720x520")
        dialog.transient(self)
        dialog.grab_set()
        dialog.grid_columnconfigure(0, weight=1)
        dialog.grid_rowconfigure(1, weight=1)

        ctk.CTkLabel(
            dialog,
            text="Systemcheck" if not startup else "Systemcheck beim Start: Aktion erforderlich",
            font=ctk.CTkFont(size=18, weight="bold"),
        ).grid(row=0, column=0, padx=18, pady=(16, 8), sticky="w")
        output = ctk.CTkTextbox(dialog)
        output.grid(row=1, column=0, padx=18, pady=8, sticky="nsew")
        output.insert("1.0", format_system_check(checks))
        output.configure(state="disabled")
        ctk.CTkButton(dialog, text="Schließen", command=dialog.destroy).grid(row=2, column=0, padx=18, pady=(8, 16), sticky="e")

    def _open_cleanup_dialog(self):
        if self.watcher and self.watcher.is_running:
            messagebox.showwarning(
                "Laufzeitdaten bereinigen",
                "Bitte stoppen Sie zuerst die Ueberwachung, damit keine laufenden Jobs geloescht werden.",
            )
            return

        dialog = ctk.CTkToplevel(self)
        dialog.title("Laufzeitdaten bereinigen")
        dialog.geometry("560x360")
        dialog.transient(self)
        dialog.grab_set()
        dialog.grid_columnconfigure(0, weight=1)

        work_var = ctk.BooleanVar(value=True)
        error_var = ctk.BooleanVar(value=False)
        logs_var = ctk.BooleanVar(value=False)
        legacy_temp_work_var = ctk.BooleanVar(value=False)

        ctk.CTkLabel(
            dialog,
            text="Laufzeitdaten bereinigen",
            font=ctk.CTkFont(size=18, weight="bold"),
        ).grid(row=0, column=0, padx=18, pady=(16, 8), sticky="w")
        ctk.CTkLabel(
            dialog,
            text="Finale Ergebnisse und Originale werden nicht geloescht.",
            text_color="gray",
        ).grid(row=1, column=0, padx=18, pady=(0, 10), sticky="w")

        options = ctk.CTkFrame(dialog)
        options.grid(row=2, column=0, padx=18, pady=8, sticky="ew")
        options.grid_columnconfigure(0, weight=1)
        ctk.CTkCheckBox(options, text="work: temporaere OCR-/Bild-Artefakte", variable=work_var).grid(row=0, column=0, padx=10, pady=8, sticky="w")
        ctk.CTkCheckBox(options, text="error: fehlgeschlagene Job-Artefakte", variable=error_var).grid(row=1, column=0, padx=10, pady=8, sticky="w")
        ctk.CTkCheckBox(options, text="logs: lokale Protokolle und Job-Historie", variable=logs_var).grid(row=2, column=0, padx=10, pady=8, sticky="w")
        ctk.CTkCheckBox(
            options,
            text="temp_work: Altbestand frueherer Versionen (enthaelt Seitenbilder)",
            variable=legacy_temp_work_var,
        ).grid(row=3, column=0, padx=10, pady=8, sticky="w")

        def run_cleanup():
            if not (work_var.get() or error_var.get() or logs_var.get() or legacy_temp_work_var.get()):
                messagebox.showinfo("Laufzeitdaten bereinigen", "Es wurde kein Bereich ausgewaehlt.")
                return
            if not messagebox.askyesno(
                "Laufzeitdaten bereinigen",
                "Ausgewaehlte Laufzeitdaten werden dauerhaft geloescht. Fortfahren?",
            ):
                return
            try:
                from core.maintenance import cleanup_runtime_artifacts

                audit = cleanup_runtime_artifacts(
                    self._app_config(),
                    include_work=work_var.get(),
                    include_error=error_var.get(),
                    include_logs=logs_var.get(),
                    include_legacy_temp_work=legacy_temp_work_var.get(),
                )
                deleted = len(audit.get("deleted", []))
                failed = len(audit.get("failed", []))
                if failed:
                    messagebox.showwarning(
                        "Laufzeitdaten bereinigen",
                        f"{deleted} Eintraege geloescht, {failed} konnten nicht geloescht werden. Details stehen im Log.",
                    )
                else:
                    messagebox.showinfo("Laufzeitdaten bereinigen", f"{deleted} Eintraege geloescht.")
                self._log(f"Laufzeitdaten bereinigt: {deleted} geloescht, {failed} fehlgeschlagen.")
                dialog.destroy()
            except Exception as exc:
                messagebox.showerror("Fehler", friendly_error_message(exc, context="Laufzeitdaten konnten nicht bereinigt werden."))

        button_frame = ctk.CTkFrame(dialog, fg_color="transparent")
        button_frame.grid(row=3, column=0, padx=18, pady=(14, 16), sticky="e")
        ctk.CTkButton(button_frame, text="Bereinigen", command=run_cleanup, width=130).grid(row=0, column=0, padx=(0, 8))
        ctk.CTkButton(button_frame, text="Abbrechen", command=dialog.destroy, width=120, fg_color="#6b7280").grid(row=0, column=1)

    # ------------------------------------------------------------------ #
    #  LLMClient aufbauen                                                  #
    # ------------------------------------------------------------------ #

    def _build_llm_client(self) -> LLMClient:
        prompts = {
            "vision":   self.vision_prompt_text.get("1.0", "end-1c").strip(),
            "fusion":   self.fusion_prompt_text.get("1.0", "end-1c").strip(),
            "analysis": self.analysis_prompt_text.get("1.0", "end-1c").strip(),
            "image_description": self.saved_prompts.get(
                "image_description",
                self.settings_manager.default_prompts["image_description"],
            ),
        }
        unload_models = self.unload_models_enabled_var.get()
        keep_alive = "0" if unload_models else "15m"
        client = LLMClient(
            vision_model   = self.vision_var.get(),
            fusion_model   = self.fusion_var.get(),
            analysis_model = self.analysis_var.get(),
            glm_ocr_model  = self.glm_ocr_var.get(),
            prompts        = prompts,
            log_callback   = self._after_log,
            think_fusion   = self.think_fusion_var.get(),
            think_analysis = self.think_analysis_var.get(),
            keep_alive     = keep_alive,
            prompt_version = self.prompt_version,
            force_pipeline = self.force_pipeline_var.get(),
            redact_cloud_inputs = self.redact_cloud_inputs_var.get(),
        )
        client.stream_callback = self._after_stream
        return client

    def _load_models(self):
        self._log("Rufe Modelle ab...")
        for var in (self.glm_ocr_var, self.vision_var, self.fusion_var, self.analysis_var):
            var.set("Wird abgerufen...")

        def fetch():
            final_models = []
            
            from core.llm.config import load_llm_config, default_llm_config_path
            config_data = load_llm_config(default_llm_config_path())
            providers = config_data.get("providers", {})

            # 1. Ollama
            ollama_host = providers.get("ollama", {}).get("api_base", "http://localhost:11434")
            if ollama_host:
                try:
                    import requests as _req
                    resp = _req.get(f"{ollama_host}/api/tags", timeout=5)
                    models = [m["name"] for m in resp.json().get("models", [])] if resp.ok else []
                except Exception:
                    try:
                        import subprocess
                        out = subprocess.run(
                            ["ollama", "list"],
                            capture_output=True,
                            text=True,
                            check=True,
                            timeout=15,
                        )
                        models = [l.split()[0] for l in out.stdout.strip().split("\n")[1:] if l.strip()]
                    except Exception:
                        models = []
                
                if models:
                    final_models.append("--- OLLAMA ---")
                    final_models.extend([f"ollama/{m}" if not m.startswith("ollama/") else m for m in models])

            # 2. Google
            google_key = providers.get("google", {}).get("api_key", "").strip()
            if google_key:
                gemini_models = []
                try:
                    import requests as _req
                    resp = _req.get(
                        "https://generativelanguage.googleapis.com/v1beta/models",
                        params={"key": google_key},
                        timeout=5,
                    )
                    if resp.ok:
                        data = resp.json()
                        for m in data.get("models", []):
                            name = m.get("name", "")
                            # Nur generative Text- und Vision-Modelle auflisten
                            if "gemini" in name.lower() and not any(x in name.lower() for x in ("embedding", "robotics", "audio", "tts", "aqa")):
                                clean_name = name
                                if clean_name.startswith("models/"):
                                    clean_name = clean_name[7:]
                                gemini_models.append(f"gemini/{clean_name}")
                        gemini_models.sort(reverse=True)
                except Exception as e:
                    self._log(f"Google API Fehler (nutze Fallback-Modellliste): {e}")

                if not gemini_models:
                    gemini_models = [
                        "gemini/gemini-3.5-flash",
                        "gemini/gemini-3.1-flash-lite",
                        "gemini/gemini-2.5-flash",
                        "gemini/gemini-2.5-flash-lite",
                        "gemini/gemini-2.5-pro",
                        "gemini/gemini-2.0-flash",
                        "gemini/gemini-2.0-flash-lite",
                    ]
                
                final_models.append("--- GOOGLE ---")
                final_models.extend(gemini_models)

            # 3. OpenAI
            openai_key = providers.get("openai", {}).get("api_key", "").strip()
            if openai_key:
                final_models.append("--- OPENAI ---")
                final_models.extend([
                    "openai/gpt-4o",
                    "openai/gpt-4o-mini",
                    "openai/o1-mini",
                ])

            # 4. Mistral
            mistral_key = providers.get("mistral", {}).get("api_key", "").strip()
            if mistral_key:
                final_models.append("--- MISTRAL ---")
                final_models.extend([
                    "mistral/mistral-large-latest",
                    "mistral/mistral-small-latest",
                    "mistral/pixtral-12b-2409",
                ])

            if not final_models:
                final_models = ["Keine Modelle/Provider gefunden"]

            self.after(0, self._update_model_dropdowns, final_models)

        threading.Thread(target=fetch, daemon=True).start()

    def _update_model_dropdowns(self, models: list):
        options = ["Keins"] + models
        is_error = ("gefunden" in models[0] or "erreichbar" in models[0]) if models else False

        for dd in (self.glm_ocr_dropdown, self.vision_dropdown, self.fusion_dropdown, self.analysis_dropdown):
            dd.configure(values=options)

        if is_error:
            for var in (self.glm_ocr_var, self.vision_var, self.fusion_var, self.analysis_var):
                var.set(models[0])
            return

        def best(*substrings):
            for s in substrings:
                m = next((m for m in models if s in m.lower() and not m.startswith("---")), None)
                if m:
                    return m
            first_real = next((m for m in models if not m.startswith("---")), models[0] if models else "Keins")
            return first_real

        defaults = {
            "glm_ocr":  best("glm-ocr") if best("glm-ocr") != "Keins" else "Keins",
            "vision":   best("qwen3-vl:8b-instruct", "qwen3-vl:4b-instruct", "vl", "vision", "gemini-3.5-flash", "gemini-2.5-flash", "gpt-4o"),
            "fusion":   best("gemma4:12b-it-qat", "gemma4:e4b-it-qat", "gemma4", "qwen", "gemini-3.5-flash", "gemini-3.1-flash-lite", "gemini-2.5-flash-lite", "gpt-4o-mini"),
            "analysis": best("gemma4:12b-it-qat", "gemma4:e4b-it-qat", "gemma4", "qwen", "gemini-3.5-flash", "gemini-3.1-flash-lite", "gemini-2.5-flash-lite", "gpt-4o-mini"),
        }

        def pick(key, var_name):
            saved = self.saved_models.get(key)
            if saved and saved in options:
                getattr(self, var_name).set(saved)
            elif saved and not saved.startswith("---"):
                getattr(self, var_name).set(saved)
            else:
                getattr(self, var_name).set(defaults[key])

        pick("glm_ocr",  "glm_ocr_var")
        pick("vision",   "vision_var")
        pick("fusion",   "fusion_var")
        pick("analysis", "analysis_var")
        self._log("Modelle geladen.")
    # ------------------------------------------------------------------ #

    def _processing_config_or_none(self, *, context: str) -> AppConfig | None:
        base_dir = self.dir_path_var.get()
        if not base_dir:
            messagebox.showerror("Fehler", "Bitte wähle einen Basis-Ordner aus.")
            return None
        try:
            config = self._app_config()
            config.ensure_directories()
        except Exception as e:
            messagebox.showerror("Fehler", friendly_error_message(e, context=context))
            return None

        if self.organize_enabled_var.get():
            from core.cloud.folder_registry import FolderRegistry
            try:
                registry = FolderRegistry(base_dir)
                if not registry.get_persons():
                    messagebox.showwarning(
                        "Einrichtung erforderlich",
                        "Es sind noch keine Ablagepfade registriert. Bitte legen Sie mindestens einen Pfad an."
                    )
                    self._add_path_dialog(onboarding=True)
                    return None
            except Exception as e:
                messagebox.showerror("Fehler", friendly_error_message(e, context="Fehler beim Laden der Ordner-Registry."))
                return None
        return config

    def _build_pipeline_orchestrator(self, config: AppConfig) -> PipelineOrchestrator:
        return PipelineOrchestrator(
            config            = config,
            llm_client        = self._build_llm_client(),
            output_format     = self.format_var.get(),
            docx_mode         = self.docx_mode_var.get(),
            log_callback      = self._after_log,
            progress_callback = self._after_progress,
            stage_callback    = self._after_workflow_status,
            organize_enabled  = self.organize_enabled_var.get(),
            confirm_sorting_each_document = self.confirm_sorting_each_document_var.get(),
            prompt_new_folder_callback = self.prompt_new_folder,
            prompt_sorting_callback = self.prompt_sorting_choice,
            gdrive_enabled    = self.gdrive_enabled_var.get(),
            gdrive_token_path = self.gdrive_token_path_var.get(),
            save_docx_enabled = self.save_docx_enabled_var.get(),
            save_json_enabled = self.save_json_enabled_var.get(),
            gdrive_upload_pdf = self.gdrive_upload_pdf_var.get(),
            gdrive_upload_docx = self.gdrive_upload_docx_var.get(),
            gdrive_upload_json = self.gdrive_upload_json_var.get(),
            synology_enabled = self.synology_enabled_var.get(),
            synology_base_url = self.synology_base_url_var.get(),
            synology_username = self.synology_username_var.get(),
            synology_password = self.synology_password_var.get(),
            synology_root_path = self.synology_root_path_var.get(),
            synology_upload_pdf = self.synology_upload_pdf_var.get(),
            synology_upload_docx = self.synology_upload_docx_var.get(),
            synology_upload_json = self.synology_upload_json_var.get(),
            review_before_save = self.review_before_save_var.get(),
            prompt_review_callback = self._prompt_review_callback,
            on_processing_start_callback = self._on_processing_start_callback,
            large_pdf_reduced  = self.large_pdf_reduced_var.get(),
            privacy_mode       = self.privacy_mode_var.get(),
            debug_artifacts_enabled = self.debug_artifacts_enabled_var.get(),
            ocr_languages       = self.ocr_languages_var.get(),
            ocr_mode            = self.ocr_mode_var.get(),
        )

    # ------------------------------------------------------------------ #
    #  Drag & Drop / manuelle Dateiuebergabe                              #
    # ------------------------------------------------------------------ #

    def _setup_drag_and_drop(self):
        if not getattr(self, "_dnd_available", False) or DND_FILES is None:
            self.drop_status_var.set("Datei auswählen")
            return

        for widget in (self, self.drop_frame, self.drop_title_label, self.drop_hint_label):
            try:
                widget.drop_target_register(DND_FILES)
                widget.dnd_bind("<<DragEnter>>", self._on_drag_enter)
                widget.dnd_bind("<<DragLeave>>", self._on_drag_leave)
                widget.dnd_bind("<<Drop>>", self._on_drop)
            except Exception:
                pass

    def _set_drop_highlight(self, active: bool):
        if active:
            self.drop_status_var.set("Loslassen zum Verarbeiten")
            self.drop_frame.configure(border_color="#2f80ed", fg_color=("#e8f2ff", "#162235"))
            return
        if not self._manual_processing_active:
            self.drop_status_var.set("Dokumente ablegen")
        self.drop_frame.configure(border_color="#6b7280", fg_color=["gray86", "gray17"])

    def _on_drag_enter(self, event):
        self._drag_depth += 1
        self._set_drop_highlight(True)
        return getattr(event, "action", "copy")

    def _on_drag_leave(self, event):
        self._drag_depth = max(0, self._drag_depth - 1)
        if self._drag_depth == 0:
            self._set_drop_highlight(False)
        return getattr(event, "action", "copy")

    def _on_drop(self, event):
        self._drag_depth = 0
        self._set_drop_highlight(False)
        self._handle_manual_input_paths(parse_drop_paths(getattr(event, "data", ""), self))
        return getattr(event, "action", "copy")

    def _select_files_for_processing(self):
        selected = filedialog.askopenfilenames(
            parent=self,
            title="Dokumente auswählen",
            filetypes=[
                ("Unterstützte Dokumente", supported_file_dialog_patterns()),
                ("Alle Dateien", "*.*"),
            ],
        )
        if selected:
            self._handle_manual_input_paths([Path(path) for path in selected])

    def _handle_manual_input_paths(self, paths: list[Path]):
        if self._manual_processing_active:
            messagebox.showinfo("Verarbeitung läuft", "Bitte warten Sie, bis die aktuelle Verarbeitung abgeschlossen ist.")
            return

        files, rejected = collect_supported_input_files(paths)
        if not files:
            messagebox.showwarning(
                "Keine verarbeitbaren Dateien",
                f"Unterstützte Dateitypen: {supported_input_suffixes_text()}",
            )
            return
        if rejected:
            self._log(f"Ignoriere {len(rejected)} nicht unterstützte Datei(en).")

        config = self._processing_config_or_none(context="Dateien konnten nicht übernommen werden.")
        if config is None:
            return

        staged_files = []
        try:
            for file_path in files:
                staged_files.append(stage_input_file(file_path, config))
        except Exception as exc:
            messagebox.showerror("Fehler", friendly_error_message(exc, context="Datei konnte nicht in den Eingang übernommen werden."))
            return

        self._save_settings(show_message=False)
        self._refresh_dashboard()
        self._log(f"{len(staged_files)} Datei(en) in den Eingang übernommen.")

        if self.watcher and self.watcher.is_running:
            self.drop_status_var.set("An Watchdog übergeben")
            return

        orchestrator = self._build_pipeline_orchestrator(config)
        self._manual_processing_active = True
        self.drop_status_var.set("Verarbeitung läuft")
        self.drop_select_btn.configure(state="disabled")

        def worker():
            outcomes = []
            try:
                for file_path in staged_files:
                    outcome = orchestrator.process_file(file_path)
                    if isinstance(outcome, dict):
                        outcomes.append(outcome)
                if getattr(orchestrator, "deferred_organizations", None):
                    orchestrator.process_deferred_organizations()
                failed = [outcome for outcome in outcomes if outcome.get("status") == "failed"]
                try:
                    from core.local_store import LocalStore

                    open_reviews = LocalStore(config).list_recoverable_work(limit=1000)
                except Exception:
                    open_reviews = [outcome for outcome in outcomes if outcome.get("review_required")]
                if failed:
                    self.after(
                        0,
                        messagebox.showerror,
                        "Verarbeitung mit Fehlern",
                        f"{len(failed)} Dokument(e) konnten nicht abgeschlossen werden. "
                        "Original und Fehlernachweise wurden im Fehlerbereich erhalten.",
                    )
                if open_reviews:
                    self.after(
                        0,
                        messagebox.showinfo,
                        "Prüfung erforderlich",
                        f"{len(open_reviews)} Dokumentpaket(e) warten sicher in der Review-Queue auf Ihre Freigabe.",
                    )
            finally:
                self._manual_processing_active = False
                self.after(0, lambda: self.drop_select_btn.configure(state="normal"))
                self.after(0, self.drop_status_var.set, "Dokumente ablegen")
                self.after(0, self._refresh_dashboard)

        self._manual_thread = threading.Thread(target=worker, daemon=True)
        self._manual_thread.start()

    def _toggle_watchdog(self):
        if self.watcher and self.watcher.is_running:
            self._stop_watchdog()
        else:
            self._start_watchdog()

    def _start_watchdog(self):
        config = self._processing_config_or_none(context="Ueberwachung konnte nicht gestartet werden.")
        if config is None:
            return
        orchestrator = self._build_pipeline_orchestrator(config)
        self.watcher = DirectoryWatcher(orchestrator)
        self._active_processing_summary = {
            "privacy_mode": orchestrator.privacy_mode,
            "redact_cloud_inputs": bool(getattr(orchestrator.llm, "redact_cloud_inputs", False)),
            "models": {
                "vision": getattr(orchestrator.llm, "vision_model", ""),
                "fusion": getattr(orchestrator.llm, "fusion_model", ""),
                "analysis": getattr(orchestrator.llm, "analysis_model", ""),
                "glm_ocr": getattr(orchestrator.llm, "glm_ocr_model", ""),
            },
        }
        self._save_settings(show_message=False)
        self.watcher.start()
        self.toggle_btn.configure(
            text="Überwachung stoppen",
            fg_color="#c93434", hover_color="#9e2a2a",
        )
        self.dir_entry.configure(state="disabled")
        self.browse_btn.configure(state="disabled")
        self.extra_inputs_btn.configure(state="disabled")
        self._refresh_dashboard()

    def _stop_watchdog(self):
        self.watcher.stop()
        self.status_watcher_var.set("Stop angefordert – laufender Job wird sicher beendet")
        self.toggle_btn.configure(state="disabled", text="Überwachung wird gestoppt...")
        threading.Thread(target=self._wait_for_watcher_stop, daemon=True).start()

    def _wait_for_watcher_stop(self):
        watcher = self.watcher
        if watcher:
            watcher.wait_until_stopped()
        self.after(0, self._watchdog_stopped_ui)

    def _watchdog_stopped_ui(self):
        self._active_processing_summary = None
        self.toggle_btn.configure(
            text="Überwachung (Watchdog) starten",
            fg_color=["#3B8ED0", "#1F6AA5"],
            state="normal",
        )
        self.status_watcher_var.set("Bereit")
        self.dir_entry.configure(state="normal")
        self.browse_btn.configure(state="normal")
        self.extra_inputs_btn.configure(state="normal")
        self._refresh_dashboard()

    # ------------------------------------------------------------------ #
    #  Logging                                                             #
    # ------------------------------------------------------------------ #

    def _log(self, message: str):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", message + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    # ------------------------------------------------------------------ #
    #  Thread-sichere Callbacks                                            #
    # ------------------------------------------------------------------ #

    def _after_progress(self, value: float):
        self.after(0, self._set_progress, value)

    def _after_workflow_status(self, event: dict):
        """Transfer structured pipeline events safely onto the Tk thread."""
        if not isinstance(event, dict):
            return
        payload = dict(event)
        payload["details"] = dict(event.get("details") or {})
        self.after(0, self._apply_workflow_status, payload)

    def _reset_workflow_status_ui(self, event: dict):
        self.workflow_active_job_id = str(event.get("job_id") or "")
        try:
            self.workflow_active_started_at = float(event.get("emitted_at_epoch") or 0.0)
        except (TypeError, ValueError):
            self.workflow_active_started_at = 0.0
        self.workflow_status_events.clear()
        source_name = str(event.get("source_name") or "").strip()
        self.workflow_document_var.set(source_name or "Aktueller Auftrag")
        self.workflow_detail_var.set(
            str(event.get("message") or "Statusanzeige für das aktuelle Dokument wurde zurückgesetzt.")
        )
        for step in WORKFLOW_STEPS:
            view = workflow_button_view(step, "pending")
            button = self.workflow_status_buttons.get(step)
            if button is not None:
                button.configure(
                    text=view["text"],
                    fg_color=view["color"],
                    hover_color=view["hover"],
                )

    def _apply_workflow_status(self, event: dict):
        """Render a validated event; stale jobs can never recolour the UI."""
        if not isinstance(event, dict) or event.get("schema") != WORKFLOW_EVENT_SCHEMA:
            return
        step = str(event.get("step") or "")
        state = str(event.get("state") or "")
        if step != "job" and step not in WORKFLOW_STEPS:
            return
        if state not in WORKFLOW_STATES:
            return
        try:
            event_time = float(event.get("emitted_at_epoch") or 0.0)
        except (TypeError, ValueError):
            event_time = 0.0

        if step == "job":
            if event_time and event_time < float(self.workflow_active_started_at or 0.0):
                return
            self._reset_workflow_status_ui(event)
            return

        job_id = str(event.get("job_id") or "")
        if not self.workflow_active_job_id and job_id:
            reset_event = dict(event)
            reset_event.update({"step": "job", "state": "running"})
            self._reset_workflow_status_ui(reset_event)
        if self.workflow_active_job_id and job_id != self.workflow_active_job_id:
            return
        if event_time and event_time < float(self.workflow_active_started_at or 0.0):
            return

        previous = self.workflow_status_events.get(step) or {}
        if previous.get("state") in WORKFLOW_TERMINAL_STATES and state == "running":
            return

        details = event.get("details") if isinstance(event.get("details"), dict) else {}
        normalized_event = {**event, "details": dict(details)}
        self.workflow_status_events[step] = normalized_event
        view = workflow_button_view(step, state, details)
        button = self.workflow_status_buttons.get(step)
        if button is not None:
            button.configure(
                text=view["text"],
                fg_color=view["color"],
                hover_color=view["hover"],
            )
        source_name = str(event.get("source_name") or "").strip()
        if source_name:
            self.workflow_document_var.set(source_name)
        message = str(event.get("message") or "").strip()
        if message:
            self.workflow_detail_var.set(message)

    def _show_workflow_status_detail(self, step: str):
        event = self.workflow_status_events.get(step)
        if not event:
            messagebox.showinfo(
                WORKFLOW_STEP_LABELS.get(step, "Verarbeitungsstatus"),
                "Dieser Schritt wurde für das aktuelle Dokument noch nicht ausgeführt.",
                parent=self,
            )
            return
        details = event.get("details") if isinstance(event.get("details"), dict) else {}
        lines = [str(event.get("message") or "Keine Detailmeldung vorhanden.")]
        target_path = str(details.get("target_path") or "").strip()
        if target_path:
            lines.append(f"\nZiel: {target_path}")
        tags = details.get("tags")
        if isinstance(tags, list) and tags:
            lines.append("Tags: " + ", ".join(str(tag) for tag in tags))
        remote_files = details.get("remote_files")
        if isinstance(remote_files, list) and remote_files:
            lines.append("\nRemote-Nachweise:")
            for item in remote_files:
                if not isinstance(item, dict):
                    continue
                name = item.get("filename") or "Datei"
                action = item.get("action") or "bestätigt"
                remote_id = item.get("drive_file_id") or item.get("remote_path") or ""
                suffix = f" · ID/Pfad: {remote_id}" if remote_id else ""
                lines.append(f"• {name}: {action}{suffix}")
        errors = details.get("validation_errors")
        if isinstance(errors, list) and errors:
            lines.append("\nPrüfhinweise:")
            lines.extend(f"• {error}" for error in errors)
        messagebox.showinfo(
            WORKFLOW_STEP_LABELS.get(step, "Verarbeitungsstatus"),
            "\n".join(lines),
            parent=self,
        )

    @staticmethod
    def _safe_close_modal(
        window,
        *,
        parent=None,
        scheduled_jobs=(),
        restore_parent_grab: bool = True,
    ) -> bool:
        """Close a Tk modal idempotently and cancel callbacks before destroy."""
        if window is None:
            return False
        try:
            if not bool(window.winfo_exists()):
                return False
        except Exception:
            return False
        for job_id in tuple(scheduled_jobs or ()):
            if not job_id:
                continue
            try:
                window.after_cancel(job_id)
            except Exception:
                pass
        try:
            window.grab_release()
        except Exception:
            pass
        try:
            window.destroy()
        except Exception:
            return False

        if parent is not None and restore_parent_grab:
            try:
                parent_exists = bool(parent.winfo_exists())
            except Exception:
                parent_exists = False
            if parent_exists:
                for method_name in ("grab_set", "lift", "focus_force"):
                    try:
                        getattr(parent, method_name)()
                    except Exception:
                        pass
        return True

    def _set_progress(self, value: float):
        normalized = max(0.0, min(float(value), 1.0))
        self.progress_var.set(normalized)
        self.progress_text_var.set(f"{int(normalized * 100)} %")
        if 0.0 < normalized < 1.0:
            self.status_watcher_var.set("Verarbeitet")
        elif self.watcher and self.watcher.is_running:
            self.status_watcher_var.set("Aktiv")
        else:
            self.status_watcher_var.set("Bereit")

    def _after_log(self, message: str):
        self.after(0, self._log, message)

    def _after_stream(self, token: str, mode):
        self.after(0, self._update_stream_ui, token, mode)

    def _update_stream_ui(self, token: str, mode):
        if mode == "clear":
            for box in (self.thinking_box, self.output_box):
                box.configure(state="normal")
                box.delete("1.0", "end")
                box.configure(state="disabled")
            return
        target = self.thinking_box if mode is True else self.output_box
        target.configure(state="normal")
        target.insert("end", token)
        target.see("end")
        target.configure(state="disabled")

    # ------------------------------------------------------------------ #
    #  Dokumentenablage & Pfadverwaltung                                  #
    # ------------------------------------------------------------------ #

    def _reload_paths_list(self):
        from core.cloud.folder_registry import FolderRegistry
        base_dir = self.dir_path_var.get()
        self.paths_box.configure(state="normal")
        self.paths_box.delete("1.0", "end")
        if base_dir:
            try:
                registry = FolderRegistry(base_dir)
                paths = registry.get_known_paths()
                self.paths_box.insert("end", "\n".join(paths))
            except Exception as e:
                self.paths_box.insert("end", f"Fehler beim Laden: {e}")
        else:
            self.paths_box.insert("end", "Bitte wählen Sie zuerst einen Basis-Ordner aus.")
        self.paths_box.configure(state="disabled")
        self._refresh_dashboard()

    def _show_onboarding_if_needed(self):
        if self.onboarding_completed or not self.organize_enabled_var.get():
            return

        base_dir = self.dir_path_var.get()
        if not base_dir:
            return

        try:
            config = self._app_config()
            config.ensure_directories()
            from core.cloud.folder_registry import FolderRegistry
            registry = FolderRegistry(base_dir)
            if registry.get_persons():
                self._on_paths_saved(mark_onboarding=True)
                return
        except Exception as e:
            self._log(f"Ersteinrichtung konnte nicht geprüft werden: {e}")
            return

        self._add_path_dialog(onboarding=True)

    def _on_paths_saved(self, created_paths=None, mark_onboarding: bool = True):
        self._reload_paths_list()
        if mark_onboarding:
            self.onboarding_completed = True
            self._save_settings(show_message=False)
        if created_paths:
            self._log(f"Ablagepfade gespeichert, {len(created_paths)} Ordner unter 'final' geprüft/erstellt.")

    def _add_path_dialog(self, onboarding: bool = False):
        base_dir = self.dir_path_var.get()
        if not base_dir:
            messagebox.showerror("Fehler", "Bitte wählen Sie zuerst einen Basis-Ordner aus.")
            return
        from ui.path_manager import PathManagerWindow
        from pathlib import Path
        try:
            PathManagerWindow(
                self,
                Path(base_dir),
                on_save_callback=lambda created=None: self._on_paths_saved(created, mark_onboarding=onboarding),
                onboarding=onboarding,
                gdrive_token_path=self.gdrive_token_path_var.get() if self.gdrive_enabled_var.get() else None,
            )
        except Exception as e:
            messagebox.showerror("Fehler", f"Konnte Pfad-Manager nicht öffnen: {e}")

    def _open_document_library(self):
        from core.local_store import LocalStore

        try:
            store = LocalStore(self.dir_path_var.get())
        except Exception as exc:
            messagebox.showerror("Fehler", f"Dokumentenbibliothek konnte nicht geöffnet werden:\n{exc}")
            return

        dialog = ctk.CTkToplevel(self)
        dialog.title("Dokumentenbibliothek")
        dialog.geometry("900x620")
        dialog.minsize(760, 500)
        dialog.transient(self)
        dialog.grab_set()
        dialog.grid_columnconfigure(0, weight=1)
        dialog.grid_rowconfigure(2, weight=1)

        ctk.CTkLabel(
            dialog,
            text="Dokumentenbibliothek",
            font=ctk.CTkFont(size=18, weight="bold"),
        ).grid(row=0, column=0, padx=18, pady=(16, 8), sticky="w")

        search_frame = ctk.CTkFrame(dialog, fg_color="transparent")
        search_frame.grid(row=1, column=0, padx=18, pady=4, sticky="ew")
        search_frame.grid_columnconfigure(0, weight=1)
        query_var = ctk.StringVar(value="")
        query_entry = ctk.CTkEntry(search_frame, textvariable=query_var, placeholder_text="Suchen nach Dateiname, Zielpfad, Typ oder Tags...")
        query_entry.grid(row=0, column=0, padx=(0, 8), sticky="ew")

        results_frame = ctk.CTkScrollableFrame(dialog)
        results_frame.grid(row=2, column=0, padx=18, pady=8, sticky="nsew")
        results_frame.grid_columnconfigure(0, weight=1)

        def open_artifact(row):
            import os

            outputs = row.get("outputs") if isinstance(row.get("outputs"), dict) else {}
            preferred = [outputs.get(key) for key in ("pdf", "docx", "txt")]
            candidate = next((Path(path) for path in preferred if path and Path(path).is_file()), None)
            if not candidate:
                messagebox.showwarning(
                    "Datei nicht gefunden",
                    "Für diesen Indexeintrag ist kein vorhandenes PDF-, DOCX- oder TXT-Artefakt verknüpft.",
                    parent=dialog,
                )
                return
            try:
                os.startfile(str(candidate))
            except Exception as exc:
                messagebox.showerror("Fehler", f"Dokument konnte nicht geöffnet werden:\n{exc}", parent=dialog)

        def open_target(row):
            target = row.get("target_path") or ""
            folder = self._app_config().final_dir.joinpath(*str(target).replace("\\", "/").split("/"))
            if not folder.is_dir():
                messagebox.showwarning("Ordner nicht gefunden", f"Der Ablageordner existiert nicht mehr:\n{folder}", parent=dialog)
                return
            self._open_folder(folder)

        def refresh():
            for child in results_frame.winfo_children():
                child.destroy()
            rows = store.search_documents(query_var.get().strip(), limit=200)
            if not rows:
                ctk.CTkLabel(results_frame, text="Keine Einträge gefunden.").grid(
                    row=0, column=0, padx=12, pady=24
                )
                return
            for index, row in enumerate(rows):
                card = ctk.CTkFrame(results_frame)
                card.grid(row=index, column=0, padx=6, pady=6, sticky="ew")
                card.grid_columnconfigure(0, weight=1)
                metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
                tags = metadata.get("tags") if isinstance(metadata.get("tags"), list) else []
                title = row.get("final_name") or row.get("source_name") or "Unbenannt"
                ctk.CTkLabel(
                    card,
                    text=title,
                    font=ctk.CTkFont(size=14, weight="bold"),
                    anchor="w",
                ).grid(row=0, column=0, padx=12, pady=(10, 2), sticky="ew")
                details = (
                    f"{metadata.get('document_date') or 'undatiert'} · "
                    f"{metadata.get('document_type') or 'Typ unbekannt'}\n"
                    f"Ziel: {row.get('target_path') or '-'}\n"
                    f"Tags: {', '.join(str(tag) for tag in tags) if tags else '-'}"
                )
                ctk.CTkLabel(
                    card,
                    text=details,
                    anchor="w",
                    justify="left",
                    wraplength=600,
                ).grid(row=1, column=0, padx=12, pady=(0, 10), sticky="ew")
                button_frame = ctk.CTkFrame(card, fg_color="transparent")
                button_frame.grid(row=0, column=1, rowspan=2, padx=12, pady=10)
                ctk.CTkButton(
                    button_frame,
                    text="Dokument",
                    width=95,
                    command=lambda current=row: open_artifact(current),
                ).pack(pady=(0, 5))
                ctk.CTkButton(
                    button_frame,
                    text="Ordner",
                    width=95,
                    fg_color="#6b7280",
                    command=lambda current=row: open_target(current),
                ).pack()

        ctk.CTkButton(search_frame, text="Suchen", width=90, command=refresh).grid(row=0, column=1, padx=4)
        ctk.CTkButton(search_frame, text="Schließen", width=90, command=dialog.destroy).grid(row=0, column=2, padx=(4, 0))
        query_entry.bind("<Return>", lambda _event: refresh())
        refresh()

    def _sync_review_artifacts(
        self,
        config: AppConfig,
        result: dict,
        item: dict,
        *,
        runner: PipelineOrchestrator | None = None,
    ) -> list[dict]:
        """Run configured remote sync for a package completed in the GUI queue."""
        runner = runner or self._build_pipeline_orchestrator(config)
        if not (runner.gdrive_enabled or runner.synology_enabled):
            return []
        runner._current_job_id = str(item.get("job_id") or "")
        runner._current_source_name = str(
            item.get("source_name") or result.get("target_path") or "Abgeschlossenes Review"
        )
        runner.report_workflow_status(
            "job",
            "running",
            "Statusanzeige für den Review-Abschluss wurde zurückgesetzt.",
            details={"reset": True, "review_resolution": True},
        )
        for step, message in (
            ("input", "Unverändertes Original und Review-Paket sind vorhanden."),
            ("ocr", "OCR-Ergebnis wurde im Review geprüft."),
            ("quality", "Menschliche Qualitätsprüfung wurde bestätigt."),
            ("metadata", "Metadaten und Tags wurden im Review bestätigt."),
            ("export", "Das vollständige Dokumentpaket wurde aktualisiert."),
            ("archive", f"Review-Paket wurde unter '{result.get('target_path') or 'Archivwurzel'}' abgelegt."),
        ):
            runner.report_workflow_status(step, "success", message)
        artifacts = {
            role: Path(path)
            for role, path in (result.get("artifacts") or {}).items()
            if path and Path(path).is_file()
        }

        def first_suffix(suffix: str, *, quality_only: bool = False):
            return next(
                (
                    path
                    for path in artifacts.values()
                    if path.suffix.lower() == suffix
                    and (not quality_only or "quality" in path.name.casefold())
                ),
                None,
            )

        pdf_file = artifacts.get("pdf") or first_suffix(".pdf")
        docx_file = (
            artifacts.get("reviewed_docx")
            or artifacts.get("docx")
            or first_suffix(".docx")
        )
        json_file = artifacts.get("json") or artifacts.get("quality") or first_suffix(
            ".json", quality_only=True
        )
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        target_path = str(result.get("target_path") or "")
        is_docx_input = bool(payload.get("is_docx"))
        uploads: list[dict] = []
        heartbeat = result.get("heartbeat")

        def renew_claim():
            if callable(heartbeat):
                heartbeat()

        if runner.gdrive_enabled:
            renew_claim()
            try:
                uploads.extend(
                    runner._stage_gdrive_upload(
                        pdf_file,
                        docx_file,
                        json_file,
                        target_path,
                        is_docx_input=is_docx_input,
                    )
                    or []
                )
            finally:
                renew_claim()
        else:
            runner.report_workflow_status(
                "google_drive",
                "skipped",
                "Google Drive ist für diesen Review-Abschluss nicht aktiviert.",
            )
        if runner.synology_enabled:
            renew_claim()
            try:
                uploads.extend(
                    runner._stage_synology_upload(
                        pdf_file,
                        docx_file,
                        json_file,
                        target_path,
                        is_docx_input=is_docx_input,
                    )
                    or []
                )
            finally:
                renew_claim()
        else:
            runner.report_workflow_status(
                "synology",
                "skipped",
                "Synology/NAS ist für diesen Review-Abschluss nicht aktiviert.",
            )
        provider_summaries = [
            (
                "google_drive",
                bool(runner.gdrive_enabled),
                getattr(runner, "_last_google_drive_summary", None),
            ),
            (
                "synology_webdav",
                bool(runner.synology_enabled),
                getattr(runner, "_last_synology_summary", None),
            ),
        ]
        # An enabled target is confirmed only by a full success summary.
        # ``skipped`` (for example because no upload format was selected) is
        # not an upload confirmation and must keep the review recoverable.
        unconfirmed_providers = [
            provider
            for provider, enabled, summary in provider_summaries
            if enabled
            and (
                not isinstance(summary, dict)
                or str(summary.get("state") or "").lower() != "success"
            )
        ]
        sync_failed = bool(unconfirmed_providers) or any(
            isinstance(entry, dict) and str(entry.get("action") or "").lower() == "failed"
            for entry in uploads
        )
        if sync_failed and not any(
            isinstance(entry, dict) and str(entry.get("action") or "").lower() == "failed"
            for entry in uploads
        ):
            uploads.append(
                {
                    "provider": "remote_sync_audit",
                    "action": "failed",
                    "error": (
                        "Der vollständige Remote-Nachweis konnte nicht bestätigt werden: "
                        + ", ".join(unconfirmed_providers or ["unbekanntes Ziel"])
                    ),
                }
            )
        runner.report_workflow_status(
            "complete",
            "error" if sync_failed else "success",
            (
                "Review lokal abgeschlossen, aber mindestens ein Remote-Ziel wurde nicht bestätigt."
                if sync_failed
                else "Review, lokale Ablage und alle aktivierten Synchronisierungen wurden bestätigt."
            ),
        )
        return uploads

    def _open_review_queue(self):
        """Open or focus the single-window review workspace."""

        current = getattr(self, "_review_queue_window", None)
        try:
            if current is not None and current.winfo_exists():
                current.present()
                return
        except Exception:
            self._review_queue_window = None

        from core.review_service import ReviewQueueService
        from ui.review_queue import ReviewQueueWindow

        try:
            service = ReviewQueueService(self._app_config())

            def clear_reference(closed_window):
                if getattr(self, "_review_queue_window", None) is closed_window:
                    self._review_queue_window = None

            window = ReviewQueueWindow(
                self,
                service,
                sync_runner_factory=lambda: self._build_pipeline_orchestrator(service.config),
                sync_artifacts=lambda config, context, item, runner: self._sync_review_artifacts(
                    config,
                    context,
                    item,
                    runner=runner,
                ),
                dashboard_refresh=self._refresh_dashboard,
                remote_sync_enabled=lambda: bool(
                    self.gdrive_enabled_var.get() or self.synology_enabled_var.get()
                ),
                on_close=clear_reference,
            )
        except Exception as exc:
            self._review_queue_window = None
            messagebox.showerror(
                "Prüfungen konnten nicht geöffnet werden",
                friendly_error_message(exc, context="Das Hauptfenster bleibt weiter bedienbar."),
                parent=self,
            )
            return
        self._review_queue_window = window

    def prompt_new_folder(self, proposed_path: str, preview_pdf_path=None) -> str | None:
        """
        Wird vom Hintergrund-Thread aufgerufen.
        Öffnet einen modalen Dialog in der GUI und blockiert, bis der Benutzer eine Auswahl getroffen hat.
        """
        result_container = []
        event = threading.Event()
        
        # UI-Erstellung muss auf dem Haupt-Thread ausgeführt werden
        self.after(0, self._show_prompt_dialog, proposed_path, preview_pdf_path, result_container, event)
        
        # Warten, bis der Benutzer eine Auswahl getroffen hat
        event.wait()
        
        if result_container:
            return result_container[0]
        return None

    def _show_prompt_dialog(self, proposed_path: str, preview_pdf_path, result_container: list, event: threading.Event):
        # Neues Toplevel-Fenster (modal)
        dialog = ctk.CTkToplevel(self)
        dialog.title("Neuer Ordner vorgeschlagen")
        dialog.geometry("620x760")
        dialog.minsize(540, 620)
        dialog.transient(self) # Immer im Vordergrund des Hauptfensters
        dialog.grab_set()      # Blockiert Interaktion mit dem Hauptfenster
        
        # Zentrieren über Hauptfenster
        dialog.update_idletasks()
        x = self.winfo_x() + (self.winfo_width() - dialog.winfo_width()) // 2
        y = self.winfo_y() + (self.winfo_height() - dialog.winfo_height()) // 2
        dialog.geometry(f"+{x}+{y}")

        # Widgets
        ctk.CTkLabel(
            dialog, text="Neuer Ordner vorgeschlagen",
            font=ctk.CTkFont(size=16, weight="bold"),
        ).pack(padx=20, pady=(15, 10))

        preview_frame = ctk.CTkFrame(dialog)
        preview_frame.pack(fill="both", expand=True, padx=20, pady=(0, 10))
        preview_frame.grid_columnconfigure(0, weight=1)
        preview_frame.grid_rowconfigure(1, weight=1)
        ctk.CTkLabel(
            preview_frame,
            text="Dokumentvorschau",
            font=ctk.CTkFont(size=13, weight="bold"),
        ).grid(row=0, column=0, padx=10, pady=(10, 4), sticky="w")
        pdf_viewer = PDFPreviewFrame(preview_frame)
        pdf_viewer.grid(row=1, column=0, padx=10, pady=(0, 10), sticky="nsew")
        pdf_viewer.configure(height=360)
        if preview_pdf_path:
            dialog.after(100, pdf_viewer.load_pdf, str(preview_pdf_path))
        else:
            pdf_viewer.show_message("Keine Dokumentvorschau verfügbar.")

        msg = (
            f"Das LLM schlägt vor, das Dokument in einen neuen Ordner\n"
            f"einzusortieren:\n\n"
            f"»  {proposed_path}  «\n\n"
            f"Wie soll verfahren werden"
        )
        ctk.CTkLabel(dialog, text=msg, justify="center").pack(padx=20, pady=10)

        # Container für Buttons
        btn_frame = ctk.CTkFrame(dialog, fg_color="transparent")
        btn_frame.pack(fill="x", padx=30, pady=10)
        
        def select_option(val):
            result_container.append(val)
            dialog.destroy()
            event.set()

        # Schließen-Event abfangen, damit der Worker nicht unendlich hängt
        def on_close():
            dialog.destroy()
            event.set()
        dialog.protocol("WM_DELETE_WINDOW", on_close)

        # Button 1: Erstellen & Nutzen
        btn_create = ctk.CTkButton(
            btn_frame, text=f"Erstellen & Nutzen ({proposed_path})",
            command=lambda: select_option(proposed_path),
            fg_color="#1f6aa5"
        )
        btn_create.pack(fill="x", pady=5)

        # Button 2: Sonstiges
        btn_misc = ctk.CTkButton(
            btn_frame, text="In 'Sonstiges' ablegen",
            command=lambda: select_option("Sonstiges"),
            fg_color="#555555"
        )
        btn_misc.pack(fill="x", pady=5)

        # Dropdown für existierende Pfade
        from core.cloud.folder_registry import FolderRegistry
        try:
            registry = FolderRegistry(self.dir_path_var.get())
            known_paths = registry.get_known_paths()
        except Exception:
            known_paths = ["Sonstiges"]

        dropdown_frame = ctk.CTkFrame(btn_frame, fg_color="transparent")
        dropdown_frame.pack(fill="x", pady=5)
        
        ctk.CTkLabel(dropdown_frame, text="Anderen Pfad wählen:").pack(side="left", padx=5)
        
        path_var = ctk.StringVar(value=known_paths[0] if known_paths else "Sonstiges")
        dropdown = ctk.CTkOptionMenu(dropdown_frame, variable=path_var, values=known_paths)
        dropdown.pack(side="left", fill="x", expand=True, padx=5)
        
        btn_select = ctk.CTkButton(
            dropdown_frame, text="Auswählen", width=80,
            command=lambda: select_option(path_var.get())
        )
        btn_select.pack(side="left", padx=5)

    def prompt_sorting_choice(self, classification_result: dict, known_paths: list, proposed_path: str, preview_pdf_path=None) -> str | None:
        result_container = []
        event = threading.Event()
        self.after(0, self._show_sorting_choice_dialog, classification_result, known_paths, proposed_path, preview_pdf_path, result_container, event)
        event.wait()
        return result_container[0] if result_container else None

    def _show_sorting_choice_dialog(self, classification_result: dict, known_paths: list, proposed_path: str, preview_pdf_path, result_container: list, event: threading.Event):
        dialog = ctk.CTkToplevel(self)
        dialog.title("Zielordner bestätigen")
        dialog.geometry("1120x680")
        dialog.minsize(900, 560)
        dialog.transient(self)
        dialog.grab_set()

        dialog.grid_columnconfigure(0, weight=1, minsize=360)
        dialog.grid_columnconfigure(1, weight=1, minsize=430)
        dialog.grid_rowconfigure(2, weight=1)

        ctk.CTkLabel(
            dialog,
            text="Zielordner bestätigen",
            font=ctk.CTkFont(size=18, weight="bold"),
        ).grid(row=0, column=0, columnspan=2, padx=20, pady=(18, 4), sticky="w")

        confidence = classification_result.get("confidence", classification_result.get("score", 0))
        reason = classification_result.get("reason", "unsicher")
        if classification_result.get("requires_confirmation"):
            prompt_text = f"Bitte bestaetige den Zielpfad. Vorschlag: {proposed_path} ({confidence} %, {reason})"
        else:
            prompt_text = f"Die automatische Einordnung ist nicht eindeutig genug. Vorschlag: {proposed_path} ({confidence} %, {reason})"
        ctk.CTkLabel(
            dialog,
            text=prompt_text,
            text_color="gray",
            wraplength=980,
        ).grid(row=1, column=0, columnspan=2, padx=20, pady=(0, 10), sticky="ew")

        preview_frame = ctk.CTkFrame(dialog)
        preview_frame.grid(row=2, column=0, padx=(20, 8), pady=8, sticky="nsew")
        preview_frame.grid_columnconfigure(0, weight=1)
        preview_frame.grid_rowconfigure(1, weight=1)
        ctk.CTkLabel(
            preview_frame,
            text="Dokumentvorschau",
            font=ctk.CTkFont(size=14, weight="bold"),
        ).grid(row=0, column=0, padx=12, pady=(12, 6), sticky="w")
        pdf_viewer = PDFPreviewFrame(preview_frame)
        pdf_viewer.grid(row=1, column=0, padx=10, pady=(0, 10), sticky="nsew")
        if preview_pdf_path:
            dialog.after(100, pdf_viewer.load_pdf, str(preview_pdf_path))
        else:
            pdf_viewer.show_message("Keine Dokumentvorschau verfügbar.")

        scroll = ctk.CTkScrollableFrame(dialog)
        scroll.grid(row=2, column=1, padx=(8, 20), pady=8, sticky="nsew")
        scroll.grid_columnconfigure(0, weight=1)

        def select(path: str):
            result_container.append(path)
            dialog.destroy()
            event.set()

        candidates = classification_result.get("candidates") or []
        shown_paths = set()
        row = 0
        for candidate in candidates[:5]:
            path = candidate.get("path") or candidate.get("recommended_path")
            if not path or path in shown_paths:
                continue
            shown_paths.add(path)
            frame = ctk.CTkFrame(scroll)
            frame.grid(row=row, column=0, padx=8, pady=6, sticky="ew")
            frame.grid_columnconfigure(0, weight=1)
            title = f"{path}   ({candidate.get('score', 0)} %)"
            ctk.CTkLabel(frame, text=title, font=ctk.CTkFont(weight="bold")).grid(row=0, column=0, padx=10, pady=(8, 2), sticky="w")
            evidence = candidate.get("evidence") or []
            if evidence:
                ctk.CTkLabel(
                    frame,
                    text="Hinweise: " + ", ".join(str(item) for item in evidence[:5]),
                    text_color="gray",
                    wraplength=420,
                ).grid(row=1, column=0, padx=10, pady=(0, 8), sticky="w")
            ctk.CTkButton(frame, text="Diesen Pfad wählen", width=140, command=lambda p=path: select(p)).grid(row=0, column=1, rowspan=2, padx=10, pady=8)
            row += 1

        manual_frame = ctk.CTkFrame(dialog)
        manual_frame.grid(row=3, column=0, columnspan=2, padx=20, pady=(8, 16), sticky="ew")
        manual_frame.grid_columnconfigure(1, weight=1)
        manual_frame.grid_columnconfigure(3, weight=0)

        paths = list(dict.fromkeys([p for p in known_paths if p] + [proposed_path, "Sonstiges"]))
        ctk.CTkLabel(manual_frame, text="Anderer Pfad:").grid(row=0, column=0, padx=10, pady=10, sticky="w")
        path_var = ctk.StringVar(value=proposed_path if proposed_path else (paths[0] if paths else "Sonstiges"))
        path_box = ctk.CTkComboBox(manual_frame, variable=path_var, values=paths)
        path_box.grid(row=0, column=1, padx=10, pady=10, sticky="ew")

        ctk.CTkLabel(manual_frame, text="Zielpfad:").grid(row=1, column=0, padx=10, pady=(0, 10), sticky="w")
        path_entry = ctk.CTkEntry(manual_frame, textvariable=path_var)
        path_entry.grid(row=1, column=1, padx=10, pady=(0, 10), sticky="ew")

        def browse_target_path():
            try:
                initial_dir = str(self._app_config().final_dir)
            except Exception:
                initial_dir = self.dir_path_var.get()
            selected = filedialog.askdirectory(initialdir=initial_dir, parent=dialog)
            if selected:
                path_var.set(selected)

        def normalize_dialog_target_path(raw_value: str) -> str:
            raw_original = str(raw_value or "").strip()
            raw = raw_original.replace("\\", "/")
            if not raw:
                raise ValueError("Bitte geben Sie einen Zielpfad an.")

            try:
                config = self._app_config()
                final_root = config.final_dir.resolve(strict=False)
                candidate = Path(raw_original).expanduser()
                if candidate.is_absolute():
                    raw = candidate.resolve(strict=False).relative_to(final_root).as_posix()
            except ValueError as exc:
                raise ValueError("Vollstaendige Pfade muessen innerhalb des final-Ordners liegen.") from exc
            except Exception:
                pass

            parts = [part.strip() for part in raw.replace("\\", "/").split("/") if part.strip()]
            if parts and parts[0].lower() == "final":
                parts = parts[1:]
            if not parts:
                raise ValueError("Bitte geben Sie einen Zielpfad an.")

            from ui.path_manager import validate_folder_name
            for part in parts:
                ok, message = validate_folder_name(part)
                if not ok:
                    raise ValueError(f"Ungueltiger Ordnername '{part}': {message}")

            from core.cloud.folder_registry import FolderRegistry
            try:
                registry = FolderRegistry(self.dir_path_var.get())
                valid_persons = registry.get_persons()
            except Exception as exc:
                raise ValueError(f"Ordner-Registry konnte nicht geladen werden: {exc}") from exc

            matched_person = next((vp for vp in valid_persons if vp.lower() == parts[0].lower()), None)
            if not matched_person:
                allowed = ", ".join(valid_persons) if valid_persons else "keine"
                raise ValueError(f"Der erste Pfadteil muss ein registrierter Hauptordner sein. Erlaubt: {allowed}")
            parts[0] = matched_person
            return "/".join(parts)

        def create_and_select_target_path():
            try:
                target_path = normalize_dialog_target_path(path_var.get())
                from core.cloud.folder_registry import FolderRegistry
                registry = FolderRegistry(self.dir_path_var.get())
                known = registry.get_known_paths()
                if target_path not in known and not registry.add_path(target_path):
                    raise ValueError("Der Zielpfad konnte nicht registriert werden.")
                (self._app_config().final_dir / Path(*target_path.split("/"))).mkdir(parents=True, exist_ok=True)
                path_var.set(target_path)
                select(target_path)
            except Exception as exc:
                messagebox.showerror("Zielpfad erstellen", str(exc), parent=dialog)

        ctk.CTkButton(
            manual_frame, text="Ordner...", width=90, command=browse_target_path
        ).grid(row=1, column=2, padx=10, pady=(0, 10))
        ctk.CTkButton(
            manual_frame, text="Uebernehmen", width=120, command=lambda: select(path_var.get())
        ).grid(row=1, column=3, padx=10, pady=(0, 10))
        ctk.CTkButton(
            manual_frame, text="Neu erstellen & nutzen", width=180, command=create_and_select_target_path
        ).grid(row=2, column=1, columnspan=3, padx=10, pady=(0, 10), sticky="e")
        ctk.CTkButton(manual_frame, text="Übernehmen", width=120, command=lambda: select(path_var.get())).grid(row=0, column=2, padx=10, pady=10)

        def on_close():
            # Closing is not confirmation. Keep the durable review item open
            # and leave the document in staging instead of silently learning
            # or filing the proposal.
            result_container.append(None)
            dialog.destroy()
            event.set()

        dialog.protocol("WM_DELETE_WINDOW", on_close)

    # ------------------------------------------------------------------ #
    #  Manuelle Prüfung & PDF-Vorschau Callbacks                        #
    # ------------------------------------------------------------------ #

    def _on_processing_start_callback(self, original_path):
        self.after(0, self.pdf_preview_panel.load_pdf, str(original_path))

    def _prompt_review_callback(
        self,
        pdf_path,
        fused_text,
        metadata,
        pre_target_path,
        quality_report=None,
        original_path=None,
    ) -> tuple:
        """
        Wird vom Hintergrund-Thread aufgerufen, wenn review_before_save aktiviert ist.
        Öffnet einen modalen Dialog und blockiert den Thread mit einem Event,
        bis der Benutzer fertig ist oder abbricht.
        """
        result_container = []
        event = threading.Event()
        # UI-Erstellung muss auf dem Haupt-Thread ausgeführt werden
        self.after(
            0,
            self._show_review_dialog,
            pdf_path,
            fused_text,
            metadata,
            pre_target_path,
            quality_report or {},
            original_path,
            result_container,
            event,
        )
        
        # Warten, bis der Benutzer fertig ist oder abbricht
        event.wait()
        
        if result_container:
            return result_container[0]
        return None

    def _show_review_dialog(
        self,
        pdf_path,
        fused_text,
        metadata,
        pre_target_path,
        quality_report,
        original_path,
        result_container,
        event,
    ):
        dialog = ctk.CTkToplevel(self)
        dialog.title("Dokumenten-Review vor dem Speichern")
        dialog.geometry("1400x850")
        dialog.minsize(1000, 600)
        dialog.transient(self) # Immer im Vordergrund des Hauptfensters
        dialog.grab_set()      # Blockiert Interaktion mit dem Hauptfenster
        preview_jobs = []
        finish_state = {"done": False}
        
        # Layout
        dialog.grid_columnconfigure(0, weight=1, minsize=500)
        dialog.grid_columnconfigure(1, weight=1, minsize=500)
        dialog.grid_rowconfigure(0, weight=1)

        # Linke Seite: PDF-Vorschau
        preview_frame = ctk.CTkFrame(dialog)
        preview_frame.grid(row=0, column=0, padx=15, pady=15, sticky="nsew")
        preview_frame.grid_columnconfigure(0, weight=1)
        preview_frame.grid_rowconfigure(1, weight=1)
        
        ctk.CTkLabel(
            preview_frame, text="Dokumentprüfung",
            font=ctk.CTkFont(size=14, weight="bold")
        ).grid(row=0, column=0, padx=10, pady=(10, 5), sticky="w")

        original_is_pdf = bool(
            original_path
            and Path(original_path).suffix.lower() == ".pdf"
            and Path(original_path) != Path(pdf_path)
        )
        if original_is_pdf:
            preview_tabs = ctk.CTkTabview(preview_frame)
            preview_tabs.grid(row=1, column=0, padx=10, pady=(0, 10), sticky="nsew")
            preview_tabs.add("Unverändertes Original")
            preview_tabs.add("OCR-Arbeits-PDF")
            for tab_name in ("Unverändertes Original", "OCR-Arbeits-PDF"):
                tab = preview_tabs.tab(tab_name)
                tab.grid_rowconfigure(0, weight=1)
                tab.grid_columnconfigure(0, weight=1)
            original_viewer = PDFPreviewFrame(preview_tabs.tab("Unverändertes Original"))
            original_viewer.grid(row=0, column=0, sticky="nsew")
            pdf_viewer = PDFPreviewFrame(preview_tabs.tab("OCR-Arbeits-PDF"))
            pdf_viewer.grid(row=0, column=0, sticky="nsew")
            preview_jobs.append(
                dialog.after(80, original_viewer.load_pdf, str(original_path))
            )
        else:
            pdf_viewer = PDFPreviewFrame(preview_frame)
            pdf_viewer.grid(row=1, column=0, padx=10, pady=(0, 10), sticky="nsew")

        preview_jobs.append(dialog.after(80, pdf_viewer.load_pdf, str(pdf_path)))

        # Rechte Seite: Metadaten & Text
        edit_scroll = ctk.CTkScrollableFrame(dialog)
        edit_scroll.grid(row=0, column=1, padx=15, pady=15, sticky="nsew")
        edit_scroll.grid_columnconfigure(0, weight=1)
        
        ctk.CTkLabel(
            edit_scroll, text="Bearbeitung & Metadaten",
            font=ctk.CTkFont(size=16, weight="bold")
        ).grid(row=0, column=0, padx=10, pady=(10, 15), sticky="w")

        quality_status = str((quality_report or {}).get("quality_status") or "unbekannt")
        quality_score = (quality_report or {}).get("quality_score")
        quality_warnings = [str(item) for item in (quality_report or {}).get("warnings", [])]
        quality_lines = [f"Status: {quality_status}"]
        if quality_score is not None:
            quality_lines[0] += f" | Score: {quality_score}/100"
        quality_lines.extend(f"• {warning}" for warning in quality_warnings[:8])
        if len(quality_warnings) > 8:
            quality_lines.append(f"• … und {len(quality_warnings) - 8} weitere Warnungen")
        quality_color = "#c93434" if quality_status == "critical" else ("#b7791f" if quality_status == "review" else "gray")
        ctk.CTkLabel(
            edit_scroll,
            text="\n".join(quality_lines),
            text_color=quality_color,
            justify="left",
            anchor="w",
            wraplength=620,
        ).grid(row=1, column=0, padx=10, pady=(0, 12), sticky="ew")

        ctk.CTkLabel(
            edit_scroll, text="Markdown Text:",
            font=ctk.CTkFont(weight="bold")
        ).grid(row=2, column=0, padx=10, pady=(5, 2), sticky="w")

        text_editor = ctk.CTkTextbox(edit_scroll, height=300)
        text_editor.grid(row=3, column=0, padx=10, pady=(0, 15), sticky="ew")
        text_editor.insert("1.0", fused_text)

        meta_frame = ctk.CTkFrame(edit_scroll, fg_color="transparent")
        meta_frame.grid(row=4, column=0, padx=10, pady=5, sticky="ew")
        meta_frame.grid_columnconfigure(1, weight=1)

        metadata_fields = [
            ("document_date", "Dokumentdatum (YYYY-MM-DD):"),
            ("title", "Titel:"),
            ("document_type", "Dokumententyp:"),
            ("tags_text", "Tags:"),
            ("issuer", "Absender/Ersteller:"),
            ("recipient", "Empfänger:"),
            ("owner", "Zuordnung/Person:"),
            ("language", "Sprache:"),
            ("amount", "Betrag:"),
            ("currency", "Währung:"),
        ]
        meta_entries = {}
        for i, (key, label_text) in enumerate(metadata_fields):
            ctk.CTkLabel(meta_frame, text=label_text).grid(row=i, column=0, padx=(0, 10), pady=6, sticky="w")
            entry = ctk.CTkEntry(meta_frame)
            entry.grid(row=i, column=1, padx=0, pady=6, sticky="ew")
            if key == "tags_text":
                from core.metadata import metadata_tags_text
                initial_value = metadata_tags_text(metadata)
            else:
                initial_value = metadata.get(key, "")
            entry.insert(0, "" if initial_value is None else str(initial_value))
            meta_entries[key] = entry

        ctk.CTkLabel(
            edit_scroll, text="Zielordner & Dateiname:",
            font=ctk.CTkFont(weight="bold")
        ).grid(row=5, column=0, padx=10, pady=(15, 2), sticky="w")

        folder_frame = ctk.CTkFrame(edit_scroll, fg_color="transparent")
        folder_frame.grid(row=6, column=0, padx=10, pady=5, sticky="ew")
        folder_frame.grid_columnconfigure(1, weight=1)

        from core.cloud.folder_registry import FolderRegistry
        try:
            registry = FolderRegistry(self.dir_path_var.get())
            known_paths = registry.get_known_paths()
        except Exception:
            known_paths = ["Sonstiges"]

        if pre_target_path not in known_paths:
            known_paths.append(pre_target_path)

        ctk.CTkLabel(folder_frame, text="Zielordner:").grid(row=0, column=0, padx=(0, 10), pady=6, sticky="w")
        folder_var = ctk.StringVar(value=pre_target_path)
        folder_menu = ctk.CTkComboBox(folder_frame, variable=folder_var, values=known_paths)
        folder_menu.grid(row=0, column=1, padx=0, pady=6, sticky="ew")

        ctk.CTkLabel(folder_frame, text="Custom Dateiname\n(optional):").grid(row=1, column=0, padx=(0, 10), pady=6, sticky="w")
        custom_name_entry = ctk.CTkEntry(folder_frame, placeholder_text="Standard (wird aus Datum_Titel_Typ generiert)")
        custom_name_entry.grid(row=1, column=1, padx=0, pady=6, sticky="ew")

        # Buttons
        btn_frame = ctk.CTkFrame(edit_scroll, fg_color="transparent")
        btn_frame.grid(row=7, column=0, padx=10, pady=(25, 10), sticky="ew")
        btn_frame.grid_columnconfigure(0, weight=1)
        btn_frame.grid_columnconfigure(1, weight=1)

        def save_and_continue():
            up_text = text_editor.get("1.0", "end-1c")
            up_meta = dict(metadata or {})
            edited_values = {k: e.get().strip() for k, e in meta_entries.items()}
            tags_text = edited_values.pop("tags_text", "")
            up_meta.update(edited_values)
            up_meta["tags"] = [
                item.strip()
                for item in re.split(r"[,;\n]+", tags_text)
                if item.strip()
            ]
            from core.metadata import normalize_metadata
            up_meta = normalize_metadata(up_meta, source_text=up_text)
            cust_name = custom_name_entry.get().strip()
            chosen_folder = folder_var.get().strip().replace("\\", "/")
            
            # Validation
            parts = [p.strip() for p in chosen_folder.split("/") if p.strip()]
            if not parts:
                messagebox.showerror("Fehler", "Bitte geben Sie einen Zielordner an.")
                return
                
            from core.cloud.folder_registry import FolderRegistry
            try:
                registry = FolderRegistry(self.dir_path_var.get())
                valid_persons = registry.get_persons()
            except Exception:
                valid_persons = []
                
            matched_person = next((vp for vp in valid_persons if vp.lower() == parts[0].lower()), None)
            if not matched_person:
                messagebox.showerror(
                    "Fehler", 
                    f"Der Hauptordner '{parts[0]}' ist nicht zulässig.\n"
                    f"Erlaubte Hauptordner sind: {', '.join(valid_persons)}"
                )
                return
                
            # Normalisiere Casing des Hauptordners
            parts[0] = matched_person
            chosen_folder = "/".join(parts)
            
            result_container.append((up_text, up_meta, cust_name, chosen_folder))
            dialog.destroy()
            event.set()

        def cancel_processing():
            dialog.destroy()
            event.set()
        btn_save = ctk.CTkButton(
            btn_frame, text="Speichern und Fortfahren",
            command=save_and_continue, fg_color="#1f6aa5", height=40
        )
        btn_save.grid(row=0, column=0, padx=(0, 5), pady=5, sticky="ew")

        btn_cancel = ctk.CTkButton(
            btn_frame, text="Abbrechen / Datei überspringen",
            command=cancel_processing, fg_color="#c93434", hover_color="#9e2a2a", height=40
        )
        btn_cancel.grid(row=0, column=1, padx=(5, 0), pady=5, sticky="ew")

        x = self.winfo_x() + (self.winfo_width() - dialog.winfo_width()) // 2
        y = self.winfo_y() + (self.winfo_height() - dialog.winfo_height()) // 2
        dialog.geometry(f"+{x}+{y}")

        dialog.protocol("WM_DELETE_WINDOW", cancel_processing)

    # ------------------------------------------------------------------ #
    #  API Settings Dialog                                               #
    # ------------------------------------------------------------------ #

    def _open_api_settings_dialog(self):
        from core.llm.config import load_llm_config, save_llm_config, default_llm_config_path
        
        dialog = ctk.CTkToplevel(self)
        dialog.title("API-Schlüssel & Provider")
        dialog.geometry("560x560")
        dialog.transient(self)
        dialog.grab_set()

        config_path = default_llm_config_path()
        config_data = load_llm_config(config_path)

        ctk.CTkLabel(dialog, text="Provider Einstellungen", font=ctk.CTkFont(size=16, weight="bold")).pack(pady=(15, 10))
        ctk.CTkLabel(
            dialog,
            text=(
                "Hinweis: Google Drive OAuth und Gemini API sind getrennt. "
                "Fuer Gemini brauchst du einen API-Key aus Google AI Studio."
            ),
            wraplength=500,
            justify="left",
        ).pack(fill="x", padx=20, pady=(0, 6))

        form_frame = ctk.CTkFrame(dialog, fg_color="transparent")
        form_frame.pack(fill="both", expand=True, padx=20, pady=10)
        form_frame.grid_columnconfigure(1, weight=1)

        providers = {
            "openai": "OpenAI API Key:",
            "google": "Google Gemini API Key:",
            "mistral": "Mistral API Key:"
        }

        entries = {}
        configured_secrets = {}
        row_idx = 0
        
        # Load keys
        prov_dict = config_data.get("providers", {})
        
        for prov_key, label_text in providers.items():
            ctk.CTkLabel(form_frame, text=label_text).grid(row=row_idx, column=0, sticky="w", pady=10, padx=(0, 10))
            
            prov_conf = prov_dict.get(prov_key, {})
            current_val = prov_conf.get("api_key", "")
            configured_secrets[prov_key] = current_val
            
            entry = ctk.CTkEntry(
                form_frame,
                show="*",
                placeholder_text=("Gespeichert – leer lassen zum Beibehalten" if current_val else "Nicht eingerichtet"),
            )
            entry.grid(row=row_idx, column=1, sticky="ew", pady=10)
            entries[prov_key] = entry
            
            row_idx += 1

        ctk.CTkLabel(form_frame, text="Ollama Host URL:").grid(row=row_idx, column=0, sticky="w", pady=10, padx=(0, 10))
        ollama_entry = ctk.CTkEntry(form_frame)
        ollama_entry.grid(row=row_idx, column=1, sticky="ew", pady=10)
        ollama_val = prov_dict.get("ollama", {}).get("api_base", "http://localhost:11434")
        ollama_entry.insert(0, ollama_val)

        status_var = ctk.StringVar(value="")
        ctk.CTkLabel(
            form_frame,
            textvariable=status_var,
            wraplength=500,
            justify="left",
            text_color="#6b7280",
        ).grid(row=row_idx + 1, column=0, columnspan=2, sticky="ew", pady=(12, 0))

        def test_gemini_key():
            google_key = entries["google"].get().strip() or configured_secrets.get("google", "")
            if not google_key:
                messagebox.showwarning("Gemini testen", "Bitte zuerst einen Google Gemini API Key eintragen.")
                return

            status_var.set("Teste Gemini API...")

            def run_test():
                try:
                    import litellm

                    response = litellm.completion(
                        model="gemini/gemini-2.5-flash-lite",
                        messages=[{"role": "user", "content": "Antworte nur mit OK."}],
                        api_key=google_key,
                        max_tokens=8,
                    )
                    content = response["choices"][0]["message"].get("content", "").strip()
                    if not content:
                        raise RuntimeError("Leere Antwort von Gemini.")
                    self.after(0, lambda: status_var.set("Gemini API erreichbar. Modelle koennen verwendet werden."))
                except Exception as exc:
                    error_text = str(exc)
                    self.after(0, lambda: status_var.set(f"Gemini-Test fehlgeschlagen: {error_text}"))

            threading.Thread(target=run_test, daemon=True).start()

        def save_api_settings():
            for prov_key in providers.keys():
                if prov_key not in config_data["providers"]:
                    config_data["providers"][prov_key] = {}
                entered = entries[prov_key].get().strip()
                config_data["providers"][prov_key]["api_key"] = entered or configured_secrets.get(prov_key, "")
                
            if "ollama" not in config_data["providers"]:
                config_data["providers"]["ollama"] = {}
            config_data["providers"]["ollama"]["api_base"] = ollama_entry.get().strip()

            try:
                save_llm_config(config_data, config_path)
                messagebox.showinfo("Gespeichert", "API-Schlüssel und Einstellungen wurden erfolgreich gespeichert.")
                dialog.destroy()
                self._load_models()
            except Exception as e:
                messagebox.showerror("Fehler", f"Konnte Einstellungen nicht speichern:\n{e}")

        btn_frame = ctk.CTkFrame(dialog, fg_color="transparent")
        btn_frame.pack(fill="x", padx=20, pady=20)
        btn_frame.grid_columnconfigure(0, weight=1)
        btn_frame.grid_columnconfigure(1, weight=1)
        btn_frame.grid_columnconfigure(2, weight=1)

        ctk.CTkButton(btn_frame, text="Gemini testen", command=test_gemini_key).grid(row=0, column=0, padx=8, sticky="ew")
        ctk.CTkButton(btn_frame, text="Speichern", command=save_api_settings).grid(row=0, column=1, padx=8, sticky="ew")
        ctk.CTkButton(btn_frame, text="Abbrechen", command=dialog.destroy, fg_color="#c93434", hover_color="#9e2a2a").grid(row=0, column=2, padx=8, sticky="ew")

        # Zentrieren
        self.update_idletasks()
        x = self.winfo_x() + (self.winfo_width() - 560) // 2
        y = self.winfo_y() + (self.winfo_height() - 560) // 2
        dialog.geometry(f"+{x}+{y}")

    # ------------------------------------------------------------------ #
    #  System Tray Integration                                            #
    # ------------------------------------------------------------------ #

    def _setup_tray(self):
        import pystray
        from pystray import MenuItem as item

        def get_watchdog_text(item):
            if hasattr(self, "watcher") and self.watcher and self.watcher.is_running:
                return "Überwachung stoppen"
            return "Überwachung starten"

        menu = pystray.Menu(
            item("Öffnen", lambda icon, item: self.after(0, self._show_window), default=True),
            item(get_watchdog_text, lambda icon, item: self.after(0, self._toggle_watchdog)),
            item("Beenden", lambda icon, item: self.after(0, self._exit_application))
        )

        self.tray_icon = pystray.Icon(
            "ocr_pipeline",
            self._create_tray_icon_image(),
            "Unified OCR & LLM Pipeline",
            menu=menu
        )
        threading.Thread(target=self.tray_icon.run, daemon=True).start()

    def _create_tray_icon_image(self):
        width = 64
        height = 64
        image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        dc = ImageDraw.Draw(image)
        dc.ellipse([4, 4, 60, 60], fill=(31, 106, 165, 255))
        dc.rectangle([20, 16, 44, 48], fill=(255, 255, 255, 255))
        dc.line([24, 24, 40, 24], fill=(31, 106, 165, 255), width=2)
        dc.line([24, 32, 40, 32], fill=(31, 106, 165, 255), width=2)
        dc.line([24, 40, 36, 40], fill=(31, 106, 165, 255), width=2)
        return image

    def _on_close_window(self):
        if self.system_tray_enabled_var.get():
            self.withdraw()
        else:
            self._exit_application()

    def _on_configure(self, event):
        if event.widget == self:
            if self.state() == "iconic" and self.system_tray_enabled_var.get():
                self.withdraw()

    def _show_window(self):
        self.deiconify()
        self.state("normal")
        self.focus_force()

    def _exit_application(self):
        if self._shutdown_pending:
            return
        if self._manual_processing_active:
            messagebox.showinfo(
                "Verarbeitung läuft",
                "Die Anwendung bleibt geöffnet, bis die manuell gestartete Verarbeitung sicher abgeschlossen ist.",
                parent=self,
            )
            return

        watcher_busy = bool(self.watcher and self.watcher.is_busy)
        if watcher_busy and not messagebox.askyesno(
            "Sicher beenden",
            "Das aktuelle Dokument wird zuerst vollständig abgeschlossen. Danach beendet sich die Anwendung. Fortfahren?",
            parent=self,
        ):
            return

        self._shutdown_pending = True
        self.status_watcher_var.set("Anwendung wird sicher beendet...")
        if self.watcher and self.watcher.is_running:
            self.watcher.stop()

        def finish_shutdown():
            if self.watcher:
                self.watcher.wait_until_stopped()
            self.after(0, self._finish_shutdown)

        threading.Thread(target=finish_shutdown, daemon=True).start()

    def _finish_shutdown(self):
        if self.tray_icon:
            try:
                self.tray_icon.stop()
            except Exception:
                pass
        self.destroy()


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
