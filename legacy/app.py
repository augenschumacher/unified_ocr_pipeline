import os
import threading
import subprocess
import json
import customtkinter as ctk
from tkinter import filedialog, messagebox

try:
    import fitz  # PyMuPDF
except ImportError:
    fitz = None

# Set appearance mode and color theme
ctk.set_appearance_mode("System")
ctk.set_default_color_theme("blue")

DEFAULT_DIR = r"A:\OCR_LLM\processed"

class App(ctk.CTk):
    def __init__(self):
        super().__init__()

        # Configure window
        self.title("OCR Text Fusion")
        self.geometry("800x750")

        # Layout
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(6, weight=1)

        # Title Label
        self.title_label = ctk.CTkLabel(self, text="OCR Text Fusion & LLM Processor", font=ctk.CTkFont(size=20, weight="bold"))
        self.title_label.grid(row=0, column=0, padx=20, pady=(10, 10))

        # --- Directory & File Selection Frame ---
        self.dir_frame = ctk.CTkFrame(self)
        self.dir_frame.grid(row=1, column=0, padx=20, pady=5, sticky="ew")
        self.dir_frame.grid_columnconfigure(1, weight=1)

        self.dir_label = ctk.CTkLabel(self.dir_frame, text="Verzeichnis:")
        self.dir_label.grid(row=0, column=0, padx=10, pady=5)

        self.dir_path_var = ctk.StringVar(value=DEFAULT_DIR)
        self.dir_entry = ctk.CTkEntry(self.dir_frame, textvariable=self.dir_path_var)
        self.dir_entry.grid(row=0, column=1, padx=10, pady=5, sticky="ew")

        self.refresh_files_btn = ctk.CTkButton(self.dir_frame, text="Aktualisieren", command=self.load_files)
        self.refresh_files_btn.grid(row=0, column=2, padx=10, pady=5)
        
        self.browse_dir_btn = ctk.CTkButton(self.dir_frame, text="Ordner wählen", command=self.browse_directory)
        self.browse_dir_btn.grid(row=0, column=3, padx=10, pady=5)

        # Scrollable Frame for Files
        self.files_scroll_frame = ctk.CTkScrollableFrame(self, label_text="Zu verarbeitende Dateien (.txt)", height=200)
        self.files_scroll_frame.grid(row=2, column=0, padx=20, pady=5, sticky="nsew")
        self.grid_rowconfigure(2, weight=2) # Give scrollable frame some weight
        
        self.file_checkboxes = []
        self.select_all_var = ctk.BooleanVar(value=False)
        self.select_all_cb = ctk.CTkCheckBox(self.files_scroll_frame, text="Alle auswählen", variable=self.select_all_var, command=self.toggle_all_files)
        self.select_all_cb.grid(row=0, column=0, padx=10, pady=(5, 10), sticky="w")

        # --- Output Naming Frame ---
        self.output_frame = ctk.CTkFrame(self)
        self.output_frame.grid(row=3, column=0, padx=20, pady=5, sticky="ew")
        self.output_frame.grid_columnconfigure(1, weight=1)

        self.output_label = ctk.CTkLabel(self.output_frame, text="Ausgabe-Suffix:")
        self.output_label.grid(row=0, column=0, padx=10, pady=10)

        self.output_suffix_var = ctk.StringVar(value="_fused")
        self.output_entry = ctk.CTkEntry(self.output_frame, textvariable=self.output_suffix_var)
        self.output_entry.grid(row=0, column=1, padx=10, pady=10, sticky="ew")

        self.full_output_label = ctk.CTkLabel(self.output_frame, text="Beispiel: originalname_fused.txt", text_color="gray")
        self.full_output_label.grid(row=1, column=0, columnspan=2, padx=10, pady=(0, 10), sticky="w")

        self.integrate_pdf_var = ctk.BooleanVar(value=True)
        self.integrate_pdf_cb = ctk.CTkCheckBox(self.output_frame, text="Text unsichtbar in Original-PDF integrieren (Google Drive Indexierung)", variable=self.integrate_pdf_var)
        self.integrate_pdf_cb.grid(row=2, column=0, columnspan=2, padx=10, pady=(0, 10), sticky="w")

        # --- Model Selection Frame ---
        self.model_frame = ctk.CTkFrame(self)
        self.model_frame.grid(row=4, column=0, padx=20, pady=5, sticky="ew")
        self.model_frame.grid_columnconfigure(1, weight=1)

        self.model_label = ctk.CTkLabel(self.model_frame, text="Ollama Modell:")
        self.model_label.grid(row=0, column=0, padx=10, pady=10)

        self.model_var = ctk.StringVar(value="Laden...")
        self.model_dropdown = ctk.CTkOptionMenu(self.model_frame, variable=self.model_var, values=["Laden..."])
        self.model_dropdown.grid(row=0, column=1, padx=10, pady=10, sticky="ew")
        
        self.refresh_models_button = ctk.CTkButton(self.model_frame, text="Modelle neu laden", command=self.load_models)
        self.refresh_models_button.grid(row=0, column=2, padx=10, pady=10)

        # --- Process Button ---
        self.process_button = ctk.CTkButton(self, text="Ausgewählte Dateien verarbeiten", command=self.start_processing, height=40, font=ctk.CTkFont(size=16, weight="bold"))
        self.process_button.grid(row=5, column=0, padx=20, pady=10, sticky="ew")

        # --- Text Output (Logs) ---
        self.log_box = ctk.CTkTextbox(self, state="disabled", height=120)
        self.log_box.grid(row=6, column=0, padx=20, pady=(0, 20), sticky="nsew")

        # Initial Loads
        self.after(100, self.load_models)
        self.after(200, self.load_files)
        
        if fitz is None:
            self.log("WARNUNG: PyMuPDF ist nicht installiert. PDF-Integration ist deaktiviert.")
            self.integrate_pdf_cb.configure(state="disabled")
            self.integrate_pdf_var.set(False)
        
    def browse_directory(self):
        dirpath = filedialog.askdirectory(initialdir=self.dir_path_var.get())
        if dirpath:
            self.dir_path_var.set(dirpath)
            self.load_files()

    def toggle_all_files(self):
        state = self.select_all_var.get()
        for cb_var, cb in self.file_checkboxes:
            if cb.winfo_exists():
                if state:
                    cb.select()
                else:
                    cb.deselect()

    def load_files(self):
        dirpath = self.dir_path_var.get()
        
        # Clear existing checkboxes (except "Select All")
        for cb_var, cb in self.file_checkboxes:
            if cb.winfo_exists():
                cb.destroy()
        self.file_checkboxes.clear()
        self.select_all_var.set(False)
        
        if not os.path.exists(dirpath) or not os.path.isdir(dirpath):
            self.log(f"Verzeichnis nicht gefunden: {dirpath}")
            return
            
        try:
            files = [f for f in os.listdir(dirpath) if f.lower().endswith(".txt")]
            # Sort files optionally
            files.sort()
            
            if not files:
                self.log(f"Keine .txt Dateien gefunden in: {dirpath}")
                return
                
            for i, file in enumerate(files):
                var = ctk.BooleanVar(value=False)
                cb = ctk.CTkCheckBox(self.files_scroll_frame, text=file, variable=var)
                cb.grid(row=i+1, column=0, padx=10, pady=2, sticky="w")
                self.file_checkboxes.append((var, cb))
                
            self.log(f"{len(files)} Dateien geladen aus: {dirpath}")
        except Exception as e:
            self.log(f"Fehler beim Laden der Dateien: {str(e)}")

    def log(self, message):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", message + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def load_models(self):
        self.log("Rufe Ollama-Modelle ab...")
        self.model_dropdown.configure(values=["Wird abgerufen..."])
        self.model_var.set("Wird abgerufen...")
        
        def fetch():
            try:
                import requests
                response = requests.get("http://localhost:11434/api/tags", timeout=5)
                if response.status_code == 200:
                    models = [model["name"] for model in response.json().get("models", [])]
                    if not models:
                        models = ["Keine Modelle gefunden"]
                else:
                    models = ["Fehler beim Abrufen der Modelle"]
            except Exception as e:
                try:
                    result = subprocess.run(["ollama", "list"], capture_output=True, text=True, check=True)
                    lines = result.stdout.strip().split('\n')[1:] # Skip header
                    models = [line.split()[0] for line in lines if line.strip()]
                    if not models:
                        models = ["Keine Modelle gefunden"]
                except:
                    models = ["Ollama läuft nicht oder nicht installiert"]
            
            self.after(0, self._update_models_ui, models)

        threading.Thread(target=fetch, daemon=True).start()
        
    def _update_models_ui(self, models):
        self.model_dropdown.configure(values=models)
        if models and models[0] not in ["Keine Modelle gefunden", "Fehler beim Abrufen der Modelle", "Ollama läuft nicht oder nicht installiert"]:
            self.model_var.set(models[0])
            self.log(f"{len(models)} Modelle gefunden.")
        else:
            self.model_var.set(models[0] if models else "")
            self.log(f"Status: {models[0] if models else 'Keine Modelle'}")

    def start_processing(self):
        dirpath = self.dir_path_var.get()
        selected_files = [cb.cget("text") for var, cb in self.file_checkboxes if var.get()]
        
        if not selected_files:
            messagebox.showerror("Fehler", "Bitte wähle mindestens eine Datei zum Verarbeiten aus.")
            return

        model = self.model_var.get()
        if not model or model in ["Wird abgerufen...", "Keine Modelle gefunden", "Fehler beim Abrufen der Modelle", "Ollama läuft nicht oder nicht installiert"]:
            messagebox.showerror("Fehler", "Bitte wähle ein gültiges Ollama-Modell aus.")
            return

        suffix = self.output_suffix_var.get()
        integrate_pdf = self.integrate_pdf_var.get()

        self.process_button.configure(state="disabled")
        self.refresh_files_btn.configure(state="disabled")
        self.browse_dir_btn.configure(state="disabled")
        self.select_all_cb.configure(state="disabled")
        for _, cb in self.file_checkboxes:
            cb.configure(state="disabled")
            
        self.log(f"--- Starte Verarbeitung von {len(selected_files)} Dateien ---")
        self.log(f"Modell: {model}")

        # Start processing thread to avoid freezing UI
        threading.Thread(target=self.process_files_sequentially, args=(dirpath, selected_files, suffix, model, integrate_pdf), daemon=True).start()

    def process_files_sequentially(self, dirpath, selected_files, suffix, model, integrate_pdf):
        try:
            import requests
            
            system_prompt = (
                "Du bist ein KI-Assistent zur medizinischen Dokumentenverarbeitung. "
                "Deine Aufgabe ist es, aus den verschiedenen OCR-Ergebnissen "
                "(Standard-OCR, Docling, Vision) einen bestmöglichen, fehlerfreien Text zusammenzustellen. "
                "Gib nur den finalen, fusionierten Text zurück, ohne zusätzliche Kommentare."
            )
            
            for i, filename in enumerate(selected_files):
                filepath = os.path.join(dirpath, filename)
                name_without_ext = os.path.splitext(filename)[0]
                output_filepath = os.path.join(dirpath, f"{name_without_ext}{suffix}.txt")
                
                self.log(f"\n[{i+1}/{len(selected_files)}] Verarbeite: {filename}")
                
                try:
                    with open(filepath, 'r', encoding='utf-8') as f:
                        content = f.read()

                    user_prompt = f"Hier sind die Daten:\n\n{content}\n\nBitte erstelle daraus einen einzigen, zusammenhängenden, fehlerfreien Text ohne Wiederholungen."
                    
                    payload = {
                        "model": model,
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_prompt}
                        ],
                        "stream": True
                    }
                    
                    self.log(f"Sende an Ollama...")
                    response = requests.post("http://localhost:11434/api/chat", json=payload, stream=True)
                    response.raise_for_status()
                    
                    fused_text = ""
                    for line in response.iter_lines():
                        if line:
                            decoded_line = line.decode('utf-8')
                            try:
                                data = json.loads(decoded_line)
                                if "message" in data and "content" in data["message"]:
                                    fused_text += data["message"]["content"]
                            except json.JSONDecodeError:
                                pass
                    
                    with open(output_filepath, 'w', encoding='utf-8') as f:
                        f.write(fused_text)
                        
                    self.log(f"-> Text gespeichert: {os.path.basename(output_filepath)}")
                    
                    # PDF Integration
                    if integrate_pdf and fitz is not None:
                        pdf_filename = name_without_ext + ".pdf"
                        pdf_filepath = os.path.join(dirpath, pdf_filename)
                        output_pdf_filepath = os.path.join(dirpath, f"{name_without_ext}{suffix}.pdf")
                        
                        if os.path.exists(pdf_filepath):
                            self.log(f"-> Integriere Text unsichtbar in: {pdf_filename}")
                            try:
                                doc = fitz.open(pdf_filepath)
                                if len(doc) > 0:
                                    page = doc[0] # Insert on first page
                                    # Insert invisible text (render_mode=3) with tiny font to avoid bounds check issues
                                    page.insert_textbox(page.rect, fused_text, fontsize=1, render_mode=3)
                                    doc.save(output_pdf_filepath)
                                    self.log(f"-> PDF erfolgreich gespeichert als: {os.path.basename(output_pdf_filepath)}")
                                else:
                                    self.log("-> Warnung: PDF hat keine Seiten.")
                                doc.close()
                            except Exception as pdf_e:
                                self.log(f"-> Fehler bei PDF-Integration: {str(pdf_e)}")
                        else:
                            self.log(f"-> Warnung: Zugehörige PDF '{pdf_filename}' nicht gefunden.")
                    
                except Exception as file_e:
                    self.log(f"-> Fehler bei {filename}: {str(file_e)}")
                    
            self.log(f"\n--- Alle {len(selected_files)} Dateien wurden verarbeitet! ---")
            
        except Exception as e:
            self.log(f"Allgemeiner Fehler: {str(e)}")
            self.after(0, lambda: messagebox.showerror("Verarbeitungsfehler", f"Ein Fehler ist aufgetreten:\n{str(e)}"))
        finally:
            self.after(0, lambda: self._enable_ui())

    def _enable_ui(self):
        self.process_button.configure(state="normal")
        self.refresh_files_btn.configure(state="normal")
        self.browse_dir_btn.configure(state="normal")
        self.select_all_cb.configure(state="normal")
        for _, cb in self.file_checkboxes:
            cb.configure(state="normal")

if __name__ == "__main__":
    app = App()
    app.mainloop()
