import threading
import subprocess
import customtkinter as ctk
from tkinter import filedialog, messagebox
from PIL import Image, ImageDraw

from core.config import AppConfig, setup_paths
from core.llm import LLMClient
from core.pipeline import PipelineOrchestrator
from core.watcher import DirectoryWatcher
from core.settings import SettingsManager
from core.runtime_paths import default_token_path, default_credentials_path
from ui.pdf_preview import PDFPreviewFrame

setup_paths()

ctk.set_appearance_mode("System")
ctk.set_default_color_theme("blue")


class App(ctk.CTk):
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
        self.format_var         = ctk.StringVar(value="PDF und DOCX")
        self.docx_mode_var      = ctk.StringVar(value="Lesbare DOCX")
        self.think_fusion_var   = ctk.BooleanVar(value=False)
        self.think_analysis_var = ctk.BooleanVar(value=False)
        self.organize_enabled_var = ctk.BooleanVar(value=True)
        self.gdrive_enabled_var = ctk.BooleanVar(value=False)
        self.gdrive_credentials_path_var = ctk.StringVar(value=str(default_credentials_path()))
        self.gdrive_token_path_var = ctk.StringVar(value=str(default_token_path()))
        self.gdrive_status_var = ctk.StringVar(value="Nicht verknüpft")
        self.save_docx_enabled_var = ctk.BooleanVar(value=True)
        self.save_json_enabled_var = ctk.BooleanVar(value=True)
        self.gdrive_upload_pdf_var = ctk.BooleanVar(value=True)
        self.gdrive_upload_docx_var = ctk.BooleanVar(value=False)
        self.gdrive_upload_json_var = ctk.BooleanVar(value=False)
        self.unload_models_enabled_var = ctk.BooleanVar(value=True)
        self.system_tray_enabled_var = ctk.BooleanVar(value=True)
        self.review_before_save_var = ctk.BooleanVar(value=False)
        self.large_pdf_reduced_var = ctk.BooleanVar(value=True)
        self.force_pipeline_var = ctk.BooleanVar(value=False)
        self.saved_models       = {}
        self.saved_prompts      = {}
        self.prompt_version     = 1
        self.onboarding_completed = False
        self.watcher = None
        self.tray_icon = None
        self._load_settings()
        if self.system_tray_enabled_var.get():
            self._setup_tray()

        self.protocol("WM_DELETE_WINDOW", self._on_close_window)
        self.bind("<Configure>", self._on_configure)

        # ================================================================ #
        #  LINKE SEITE â€“ TabView mit scrollbaren Tabs                      #
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

        # â”€â”€ Ordner-Auswahl â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        dir_frame = ctk.CTkFrame(self.scroll_main)
        dir_frame.grid(row=0, column=0, padx=12, pady=(12, 6), sticky="ew")
        dir_frame.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(dir_frame, text="Basis-Ordner:").grid(row=0, column=0, padx=10, pady=10)
        self.dir_entry = ctk.CTkEntry(dir_frame, textvariable=self.dir_path_var)
        self.dir_entry.grid(row=0, column=1, padx=10, pady=10, sticky="ew")
        self.browse_btn = ctk.CTkButton(dir_frame, text="Ändern", command=self._browse_dir, width=90)
        self.browse_btn.grid(row=0, column=2, padx=(0, 10), pady=10)
        ctk.CTkLabel(
            dir_frame,
            text="Ordner 'consume', 'original' und 'final' werden automatisch erstellt.",
            text_color="gray", font=ctk.CTkFont(size=11),
        ).grid(row=1, column=0, columnspan=3, padx=10, pady=(0, 8), sticky="w")

        # â”€â”€ Modell-Auswahl â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        llm_frame = ctk.CTkFrame(self.scroll_main)
        llm_frame.grid(row=1, column=0, padx=12, pady=6, sticky="ew")
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
        fmt_frame.grid(row=2, column=0, padx=12, pady=6, sticky="ew")
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
        ctrl_frame.grid(row=3, column=0, padx=12, pady=6, sticky="ew")
        ctrl_frame.grid_columnconfigure(0, weight=1)

        self.toggle_btn = ctk.CTkButton(
            ctrl_frame,
            text="Überwachung (Watchdog) starten",
            command=self._toggle_watchdog,
            height=42, font=ctk.CTkFont(size=15, weight="bold"),
        )
        self.toggle_btn.grid(row=0, column=0, padx=16, pady=14, sticky="ew")

        # â”€â”€ Fortschrittsbalken â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        self.progress_var = ctk.DoubleVar(value=0.0)
        prog_bar = ctk.CTkProgressBar(self.scroll_main, variable=self.progress_var)
        prog_bar.grid(row=4, column=0, padx=12, pady=(0, 6), sticky="ew")

        # â”€â”€ Log-Box â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        # Feste Höhe: die Box hat ihren eigenen Scrollbalken
        self.log_box = ctk.CTkTextbox(self.scroll_main, height=260, state="disabled")
        self.log_box.grid(row=5, column=0, padx=12, pady=(0, 12), sticky="ew")

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

        # â”€â”€ Große PDFs (> 20 Seiten) Sektion â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        ctk.CTkLabel(
            self.scroll_settings, text="Große PDFs (> 20 Seiten):",
            font=ctk.CTkFont(size=13, weight="bold")
        ).grid(row=12, column=0, padx=10, pady=(15, 2), sticky="w")

        large_pdf_frame = ctk.CTkFrame(self.scroll_settings)
        large_pdf_frame.grid(row=13, column=0, padx=10, pady=5, sticky="ew")
        large_pdf_frame.grid_columnconfigure(0, weight=1)

        ctk.CTkCheckBox(
            large_pdf_frame, text="Reduzierte Analyse bei großen PDFs (> 20 Seiten) aktivieren",
            variable=self.large_pdf_reduced_var,
        ).grid(row=0, column=0, padx=10, pady=7, sticky="w")

        # â”€â”€ System-Optionen Sektion â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        ctk.CTkLabel(
            self.scroll_settings, text="System-Optionen:",
            font=ctk.CTkFont(size=13, weight="bold")
        ).grid(row=14, column=0, padx=10, pady=(15, 2), sticky="w")

        system_options_frame = ctk.CTkFrame(self.scroll_settings)
        system_options_frame.grid(row=15, column=0, padx=10, pady=5, sticky="ew")
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
            system_options_frame, text="Pipeline erzwingen (Cache ignorieren)",
            variable=self.force_pipeline_var,
        ).grid(row=3, column=0, padx=10, pady=(0, 7), sticky="w")

        # â”€â”€ API & Provider Einstellungen â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        ctk.CTkLabel(
            self.scroll_settings, text="API & Provider Einstellungen:",
            font=ctk.CTkFont(size=13, weight="bold")
        ).grid(row=16, column=0, padx=10, pady=(15, 2), sticky="w")

        api_frame = ctk.CTkFrame(self.scroll_settings)
        api_frame.grid(row=17, column=0, padx=10, pady=5, sticky="ew")
        api_frame.grid_columnconfigure(0, weight=1)
        
        ctk.CTkButton(
            api_frame, text="API-Schlüssel verwalten...",
            command=self._open_api_settings_dialog, width=250
        ).grid(row=0, column=0, padx=10, pady=10, sticky="w")

        # Speicher-Button nach unten verschoben
        ctk.CTkButton(
            self.scroll_settings, text="Einstellungen speichern",
            command=self._save_settings_clicked,
        ).grid(row=18, column=0, padx=10, pady=18)

        # Prompts befüllen
        defaults = self.settings_manager.default_prompts
        self.vision_prompt_text.insert("1.0",   self.saved_prompts.get("vision",   defaults["vision"]))
        self.fusion_prompt_text.insert("1.0",   self.saved_prompts.get("fusion",   defaults["fusion"]))
        self.analysis_prompt_text.insert("1.0", self.saved_prompts.get("analysis", defaults["analysis"]))

        # ================================================================ #
        #  RECHTE SEITE â€“ Streaming-Panel mit scrollbarem Wrapper          #
        # ================================================================ #
        # Äußerer Rahmen (bleibt fest im Haupt-Grid)
        right_outer = ctk.CTkFrame(self)
        right_outer.grid(row=1, column=1, padx=(8, 16), pady=(0, 16), sticky="nsew")
        right_outer.grid_columnconfigure(0, weight=1)
        right_outer.grid_rowconfigure(1, weight=1)   # scroll_stream wächst

        # Überschrift (außerhalb des Scrollbereichs â€“ immer sichtbar)
        ctk.CTkLabel(
            right_outer, text="LLM Live-Ausgabe",
            font=ctk.CTkFont(size=15, weight="bold"),
        ).grid(row=0, column=0, padx=14, pady=(14, 4), sticky="w")

        # Scrollbarer Container für Thinking + Output
        stream_scroll = ctk.CTkScrollableFrame(right_outer)
        stream_scroll.grid(row=1, column=0, padx=6, pady=(0, 10), sticky="nsew")
        stream_scroll.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            stream_scroll, text="🧠 Denkprozess (Chain of Thought):",
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
            stream_scroll, text="✨ Generierte Ausgabe:",
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
        #  RECHTE SEITE 2 â€“ PDF-Vorschau Panel                             #
        # ================================================================ #
        preview_outer = ctk.CTkFrame(self)
        preview_outer.grid(row=1, column=2, padx=(8, 16), pady=(0, 16), sticky="nsew")
        preview_outer.grid_columnconfigure(0, weight=1)
        preview_outer.grid_rowconfigure(1, weight=1)

        ctk.CTkLabel(
            preview_outer, text="📄 PDF-Vorschau",
            font=ctk.CTkFont(size=15, weight="bold"),
        ).grid(row=0, column=0, padx=14, pady=(14, 4), sticky="w")

        self.pdf_preview_panel = PDFPreviewFrame(preview_outer)
        self.pdf_preview_panel.grid(row=1, column=0, padx=10, pady=(0, 10), sticky="nsew")

        self.watcher = None
        self.after(120, self._load_models)
        self.after(150, self._reload_paths_list)
        self.after(350, self._show_onboarding_if_needed)

    # ------------------------------------------------------------------ #
    #  Settings laden / speichern                                          #
    # ------------------------------------------------------------------ #

    def _load_settings(self):
        s = self.settings_manager.settings
        self.dir_path_var.set(s.get("base_dir", r"C:\OCR_Workdir"))
        self.format_var.set(s.get("output_format", "PDF und DOCX"))
        self.docx_mode_var.set(s.get("docx_mode", "Lesbare DOCX"))
        self.think_fusion_var.set(s.get("think_fusion", False))
        self.think_analysis_var.set(s.get("think_analysis", False))
        self.organize_enabled_var.set(s.get("organize_enabled", True))
        self.gdrive_enabled_var.set(s.get("gdrive_enabled", False))
        self.gdrive_credentials_path_var.set(s.get("gdrive_credentials_path", "credentials.json"))
        self.gdrive_token_path_var.set(s.get("gdrive_token_path", str(default_token_path())))
        self.save_docx_enabled_var.set(s.get("save_docx_enabled", True))
        self.save_json_enabled_var.set(s.get("save_json_enabled", True))
        self.gdrive_upload_pdf_var.set(s.get("gdrive_upload_pdf", True))
        self.gdrive_upload_docx_var.set(s.get("gdrive_upload_docx", False))
        self.gdrive_upload_json_var.set(s.get("gdrive_upload_json", False))
        self.unload_models_enabled_var.set(s.get("unload_models_enabled", True))
        self.system_tray_enabled_var.set(s.get("system_tray_enabled", True))
        self.review_before_save_var.set(s.get("review_before_save", False))
        self.large_pdf_reduced_var.set(s.get("large_pdf_reduced", True))
        self.force_pipeline_var.set(s.get("force_pipeline", False))
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

    def _browse_dir(self):
        path = filedialog.askdirectory(initialdir=self.dir_path_var.get())
        if path:
            self.dir_path_var.set(path)
            self._save_settings(show_message=False)
            self._reload_paths_list()

    def _save_settings_clicked(self):
        self._save_settings(show_message=True)
        self._reload_paths_list()

    def _save_settings(self, show_message: bool = False):
        settings = {
            "base_dir":       self.dir_path_var.get(),
            "output_format":  self.format_var.get(),
            "docx_mode":      self.docx_mode_var.get(),
            "think_fusion":   self.think_fusion_var.get(),
            "think_analysis": self.think_analysis_var.get(),
            "organize_enabled": self.organize_enabled_var.get(),
            "gdrive_enabled": self.gdrive_enabled_var.get(),
            "gdrive_credentials_path": self.gdrive_credentials_path_var.get(),
            "gdrive_token_path": self.gdrive_token_path_var.get(),
            "save_docx_enabled": self.save_docx_enabled_var.get(),
            "save_json_enabled": self.save_json_enabled_var.get(),
            "gdrive_upload_pdf": self.gdrive_upload_pdf_var.get(),
            "gdrive_upload_docx": self.gdrive_upload_docx_var.get(),
            "gdrive_upload_json": self.gdrive_upload_json_var.get(),
            "unload_models_enabled": self.unload_models_enabled_var.get(),
            "system_tray_enabled": self.system_tray_enabled_var.get(),
            "review_before_save": self.review_before_save_var.get(),
            "large_pdf_reduced": self.large_pdf_reduced_var.get(),
            "force_pipeline": self.force_pipeline_var.get(),
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
                messagebox.showinfo("Gespeichert", "Einstellungen wurden gespeichert.")
        except Exception as e:
            messagebox.showerror("Fehler", f"Konnte Einstellungen nicht speichern:\n{e}")

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
                        out = subprocess.run(["ollama", "list"], capture_output=True, text=True, check=True)
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
                    resp = _req.get(f"https://generativelanguage.googleapis.com/v1beta/models?key={google_key}", timeout=5)
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
            "vision":   best("vl", "vision", "gemini-3.5-flash", "gemini-2.5-flash", "gpt-4o"),
            "fusion":   best("qwen3.6", "qwen", "gemini-3.5-flash", "gemini-3.1-flash-lite", "gemini-2.5-flash-lite", "gpt-4o-mini"),
            "analysis": best("qwen3.6", "qwen", "gemini-3.5-flash", "gemini-3.1-flash-lite", "gemini-2.5-flash-lite", "gpt-4o-mini"),
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

    def _toggle_watchdog(self):
        if self.watcher and self.watcher.is_running:
            self._stop_watchdog()
        else:
            self._start_watchdog()

    def _start_watchdog(self):
        base_dir = self.dir_path_var.get()
        if not base_dir:
            messagebox.showerror("Fehler", "Bitte wähle einen Basis-Ordner aus.")
            return
        config = AppConfig(base_dir)
        config.ensure_directories()
        
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
                    return
            except Exception as e:
                messagebox.showerror("Fehler", f"Fehler beim Laden der Ordner-Registry: {e}")
                return
        orchestrator = PipelineOrchestrator(
            config            = config,
            llm_client        = self._build_llm_client(),
            output_format     = self.format_var.get(),
            docx_mode         = self.docx_mode_var.get(),
            log_callback      = self._after_log,
            progress_callback = self._after_progress,
            organize_enabled  = self.organize_enabled_var.get(),
            prompt_new_folder_callback = self.prompt_new_folder,
            gdrive_enabled    = self.gdrive_enabled_var.get(),
            gdrive_token_path = self.gdrive_token_path_var.get(),
            save_docx_enabled = self.save_docx_enabled_var.get(),
            save_json_enabled = self.save_json_enabled_var.get(),
            gdrive_upload_pdf = self.gdrive_upload_pdf_var.get(),
            gdrive_upload_docx = self.gdrive_upload_docx_var.get(),
            gdrive_upload_json = self.gdrive_upload_json_var.get(),
            review_before_save = self.review_before_save_var.get(),
            prompt_review_callback = self._prompt_review_callback,
            on_processing_start_callback = self._on_processing_start_callback,
            large_pdf_reduced  = self.large_pdf_reduced_var.get(),
        )
        self.watcher = DirectoryWatcher(orchestrator)
        self._save_settings(show_message=False)
        self.watcher.start()
        self.toggle_btn.configure(
            text="Überwachung stoppen",
            fg_color="#c93434", hover_color="#9e2a2a",
        )
        self.dir_entry.configure(state="disabled")
        self.browse_btn.configure(state="disabled")

    def _stop_watchdog(self):
        self.watcher.stop()
        self.toggle_btn.configure(
            text="Überwachung (Watchdog) starten",
            fg_color=["#3B8ED0", "#1F6AA5"],
        )
        self.dir_entry.configure(state="normal")
        self.browse_btn.configure(state="normal")

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
        self.after(0, self.progress_var.set, value)

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

    def _show_onboarding_if_needed(self):
        if self.onboarding_completed or not self.organize_enabled_var.get():
            return

        base_dir = self.dir_path_var.get()
        if not base_dir:
            return

        try:
            config = AppConfig(base_dir)
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

    def prompt_new_folder(self, proposed_path: str) -> str:
        """
        Wird vom Hintergrund-Thread aufgerufen.
        Öffnet einen modalen Dialog in der GUI und blockiert, bis der Benutzer eine Auswahl getroffen hat.
        """
        result_container = []
        event = threading.Event()
        
        # UI-Erstellung muss auf dem Haupt-Thread ausgeführt werden
        self.after(0, self._show_prompt_dialog, proposed_path, result_container, event)
        
        # Warten, bis der Benutzer eine Auswahl getroffen hat
        event.wait()
        
        if result_container:
            return result_container[0]
        return "Sonstiges"

    def _show_prompt_dialog(self, proposed_path: str, result_container: list, event: threading.Event):
        # Neues Toplevel-Fenster (modal)
        dialog = ctk.CTkToplevel(self)
        dialog.title("Neuer Ordner vorgeschlagen")
        dialog.geometry("500x320")
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

        msg = (
            f"Das LLM schlägt vor, das Dokument in einen neuen Ordner\n"
            f"einzusortieren:\n\n"
            f"Â»  {proposed_path}  Â«\n\n"
            f"Wie soll verfahren werden?"
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
            result_container.append("Sonstiges")
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

    # ------------------------------------------------------------------ #
    #  Manuelle Prüfung & PDF-Vorschau Callbacks                        #
    # ------------------------------------------------------------------ #

    def _on_processing_start_callback(self, original_path):
        self.after(0, self.pdf_preview_panel.load_pdf, str(original_path))

    def _prompt_review_callback(self, pdf_path, fused_text, metadata, pre_target_path) -> tuple:
        """
        Wird vom Hintergrund-Thread aufgerufen, wenn review_before_save aktiviert ist.
        Öffnet einen modalen Dialog und blockiert den Thread mit einem Event,
        bis der Benutzer fertig ist oder abbricht.
        """
        result_container = []
        event = threading.Event()
        # UI-Erstellung muss auf dem Haupt-Thread ausgeführt werden
        self.after(0, self._show_review_dialog, pdf_path, fused_text, metadata, pre_target_path, result_container, event)
        
        # Warten, bis der Benutzer fertig ist oder abbricht
        event.wait()
        
        if result_container:
            return result_container[0]
        return None

    def _show_review_dialog(self, pdf_path, fused_text, metadata, pre_target_path, result_container, event):
        dialog = ctk.CTkToplevel(self)
        dialog.title("Dokumenten-Review vor dem Speichern")
        dialog.geometry("1400x850")
        dialog.minsize(1000, 600)
        dialog.transient(self) # Immer im Vordergrund des Hauptfensters
        dialog.grab_set()      # Blockiert Interaktion mit dem Hauptfenster
        
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
            preview_frame, text="📄 Original-Dokument",
            font=ctk.CTkFont(size=14, weight="bold")
        ).grid(row=0, column=0, padx=10, pady=(10, 5), sticky="w")
        
        pdf_viewer = PDFPreviewFrame(preview_frame)
        pdf_viewer.grid(row=1, column=0, padx=10, pady=(0, 10), sticky="nsew")
        
        dialog.update_idletasks()
        pdf_viewer.load_pdf(str(pdf_path))

        # Rechte Seite: Metadaten & Text
        edit_scroll = ctk.CTkScrollableFrame(dialog)
        edit_scroll.grid(row=0, column=1, padx=15, pady=15, sticky="nsew")
        edit_scroll.grid_columnconfigure(0, weight=1)
        
        ctk.CTkLabel(
            edit_scroll, text="Bearbeitung & Metadaten",
            font=ctk.CTkFont(size=16, weight="bold")
        ).grid(row=0, column=0, padx=10, pady=(10, 15), sticky="w")

        ctk.CTkLabel(
            edit_scroll, text="Markdown Text:",
            font=ctk.CTkFont(weight="bold")
        ).grid(row=1, column=0, padx=10, pady=(5, 2), sticky="w")

        text_editor = ctk.CTkTextbox(edit_scroll, height=300)
        text_editor.grid(row=2, column=0, padx=10, pady=(0, 15), sticky="ew")
        text_editor.insert("1.0", fused_text)

        meta_frame = ctk.CTkFrame(edit_scroll, fg_color="transparent")
        meta_frame.grid(row=3, column=0, padx=10, pady=5, sticky="ew")
        meta_frame.grid_columnconfigure(1, weight=1)

        metadata_fields = [
            ("date", "Datum (DD-MM-YYYY):"),
            ("title", "Titel (ohne Leerzeichen):"),
            ("document_type", "Dokumententyp:"),
            ("tags", "Tags (kommagetrennt):")
        ]
        meta_entries = {}
        for i, (key, label_text) in enumerate(metadata_fields):
            ctk.CTkLabel(meta_frame, text=label_text).grid(row=i, column=0, padx=(0, 10), pady=6, sticky="w")
            entry = ctk.CTkEntry(meta_frame)
            entry.grid(row=i, column=1, padx=0, pady=6, sticky="ew")
            entry.insert(0, metadata.get(key, ""))
            meta_entries[key] = entry

        ctk.CTkLabel(
            edit_scroll, text="Zielordner & Dateiname:",
            font=ctk.CTkFont(weight="bold")
        ).grid(row=4, column=0, padx=10, pady=(15, 2), sticky="w")

        folder_frame = ctk.CTkFrame(edit_scroll, fg_color="transparent")
        folder_frame.grid(row=5, column=0, padx=10, pady=5, sticky="ew")
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
        btn_frame.grid(row=6, column=0, padx=10, pady=(25, 10), sticky="ew")
        btn_frame.grid_columnconfigure(0, weight=1)
        btn_frame.grid_columnconfigure(1, weight=1)

        def save_and_continue():
            up_text = text_editor.get("1.0", "end-1c")
            up_meta = {k: e.get().strip() for k, e in meta_entries.items()}
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
        import yaml
        from core.llm.config import load_llm_config, default_llm_config_path
        
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
        row_idx = 0
        
        # Load keys
        prov_dict = config_data.get("providers", {})
        
        for prov_key, label_text in providers.items():
            ctk.CTkLabel(form_frame, text=label_text).grid(row=row_idx, column=0, sticky="w", pady=10, padx=(0, 10))
            
            prov_conf = prov_dict.get(prov_key, {})
            current_val = prov_conf.get("api_key", "")
            
            entry = ctk.CTkEntry(form_frame, show="*")
            entry.grid(row=row_idx, column=1, sticky="ew", pady=10)
            entry.insert(0, current_val)
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
            google_key = entries["google"].get().strip()
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
                config_data["providers"][prov_key]["api_key"] = entries[prov_key].get().strip()
                
            if "ollama" not in config_data["providers"]:
                config_data["providers"]["ollama"] = {}
            config_data["providers"]["ollama"]["api_base"] = ollama_entry.get().strip()

            try:
                config_path.parent.mkdir(parents=True, exist_ok=True)
                with open(config_path, "w", encoding="utf-8") as f:
                    yaml.safe_dump(config_data, f, default_flow_style=False, sort_keys=False)
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
        if self.watcher and self.watcher.is_running:
            self.watcher.stop()
        if self.tray_icon:
            self.tray_icon.stop()
        self.destroy()
        import os
        os._exit(0)


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
