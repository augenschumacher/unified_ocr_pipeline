from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from tkinter import messagebox, ttk
import tkinter as tk


APP_NAME = "Unified OCR"
APP_FOLDER = "UnifiedOCR"
EXE_NAME = "UnifiedOCR.exe"
PAYLOAD_NAME = "unifiedocr_payload.zip"
MODEL_CATALOG_NAME = "ollama_model_recommendations.json"
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

REQUIRED_PACKAGES = (
    {
        "label": "Tesseract OCR",
        "commands": ("tesseract",),
        "winget_id": "UB-Mannheim.TesseractOCR",
    },
    {
        "label": "Ghostscript",
        "commands": ("gswin64c", "gswin32c", "gs"),
        "winget_id": "ArtifexSoftware.GhostScript",
    },
    {
        "label": "QPDF",
        "commands": ("qpdf",),
        "winget_id": "QPDF.QPDF",
    },
)

FALLBACK_MODEL_RECOMMENDATIONS = {
    8: {
        "label": "8 GB VRAM - fluessig und konservativ",
        "glm_ocr": "glm-ocr:bf16",
        "vision": "qwen3-vl:4b-instruct-q4_K_M",
        "fusion": "gemma4:e4b-it-qat",
        "analysis": "gemma4:e4b-it-qat",
        "estimated_download_gb": 18,
        "required_free_gb": 25,
        "notes": "Kleine Vision-Stufe, Gemma4 QAT fuer Text-Fusion/Analyse.",
    },
    12: {
        "label": "12 GB VRAM - ausgewogen",
        "glm_ocr": "glm-ocr:bf16",
        "vision": "qwen3-vl:8b-instruct-q4_K_M",
        "fusion": "gemma4:12b-it-qat",
        "analysis": "gemma4:12b-it-qat",
        "estimated_download_gb": 30,
        "required_free_gb": 40,
        "notes": "Bessere Vision-Qualitaet und 12B-QAT fuer Textaufgaben.",
    },
    16: {
        "label": "16 GB VRAM - stark fuer lange Dokumente",
        "glm_ocr": "glm-ocr:bf16",
        "vision": "qwen3-vl:8b-instruct-q4_K_M",
        "fusion": "gemma4:12b-it-qat",
        "analysis": "gemma4:12b-it-qat",
        "estimated_download_gb": 30,
        "required_free_gb": 40,
        "notes": "Gute Reserve fuer grosse Seitenbilder und 256K-Kontextmodelle.",
    },
    24: {
        "label": "24 GB VRAM - grosse MoE/QAT-Stufe",
        "glm_ocr": "glm-ocr:bf16",
        "vision": "qwen3-vl:30b-a3b-instruct-q4_K_M",
        "fusion": "gemma4:26b-a4b-it-qat",
        "analysis": "gemma4:26b-a4b-it-qat",
        "estimated_download_gb": 65,
        "required_free_gb": 85,
        "notes": "MoE/QAT-Empfehlung; Modelle sollten nacheinander entladen werden.",
    },
    32: {
        "label": "32 GB VRAM - maximale lokale Qualitaet",
        "glm_ocr": "glm-ocr:bf16",
        "vision": "qwen3-vl:32b-instruct-q4_K_M",
        "fusion": "gemma4:31b-it-qat",
        "analysis": "gemma4:31b-it-qat",
        "estimated_download_gb": 82,
        "required_free_gb": 105,
        "notes": "Sehr starke lokale Modelle; grosse Downloads und viel Plattenplatz.",
    },
}


def resource_path(name: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / name


def load_model_recommendations(catalog_path: str | Path | None = None) -> dict[int, dict]:
    if catalog_path:
        candidates = [Path(catalog_path)]
    else:
        candidates = [
            resource_path(MODEL_CATALOG_NAME),
            Path(__file__).resolve().parents[2] / "unified_ocr_app" / "resources" / MODEL_CATALOG_NAME,
        ]
    for path in candidates:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            tiers = {}
            for item in data.get("tiers", []):
                rec = dict(item)
                tiers[int(rec["vram_gb"])] = rec
            if tiers:
                return dict(sorted(tiers.items()))
        except Exception:
            continue
    return FALLBACK_MODEL_RECOMMENDATIONS


MODEL_RECOMMENDATIONS = load_model_recommendations()


def install_dir() -> Path:
    root = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(root) / "Programs" / APP_FOLDER


def appdata_dir() -> Path:
    root = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(root) / APP_FOLDER


def log_path() -> Path:
    path = appdata_dir() / "install.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def log(message: str) -> None:
    with log_path().open("a", encoding="utf-8") as handle:
        handle.write(message.rstrip() + "\n")


def _path_inside(child: Path, parent: Path) -> bool:
    child_key = os.path.normcase(str(child.resolve(strict=False)))
    parent_key = os.path.normcase(str(parent.resolve(strict=False)))
    try:
        return os.path.commonpath([child_key, parent_key]) == parent_key
    except ValueError:
        return False


def _make_writable_and_retry(function, path: str, _exc_info) -> None:
    try:
        os.chmod(path, stat.S_IWRITE)
        function(path)
    except OSError:
        raise


def remove_directory_retry(path: Path, allowed_parent: Path, attempts: int = 8) -> bool:
    path = path.resolve(strict=False)
    allowed_parent = allowed_parent.resolve(strict=False)
    if not path.exists():
        return True
    if path == allowed_parent or not _path_inside(path, allowed_parent):
        raise RuntimeError(f"Unsicherer Loeschpfad: {path}")

    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            shutil.rmtree(path, onerror=_make_writable_and_retry)
        except Exception as exc:
            last_error = exc
            log(f"Could not remove {path} on attempt {attempt + 1}: {exc}")

        if not path.exists():
            return True
        time.sleep(0.5 + attempt * 0.25)

    raise RuntimeError(
        "Ordner konnte nicht vollstaendig entfernt werden. "
        f"Pfad: {path}. Letzter Fehler: {last_error}"
    )


def app_is_running() -> bool:
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {EXE_NAME}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            creationflags=CREATE_NO_WINDOW,
            check=False,
        )
        return EXE_NAME.lower() in (result.stdout or "").lower()
    except Exception as exc:
        log(f"Could not inspect running processes: {exc}")
        return False


def close_running_app(root: tk.Tk | None) -> bool:
    if not app_is_running():
        return True

    if root is not None:
        allowed = messagebox.askyesno(
            APP_NAME,
            "Eine alte Unified-OCR-Instanz laeuft noch und blockiert die Aktualisierung.\n\n"
            "Soll der Installer die laufende App jetzt schliessen?",
            parent=root,
        )
        if not allowed:
            return False

    run_logged(["taskkill", "/IM", EXE_NAME, "/T", "/F"])
    for _ in range(12):
        time.sleep(0.5)
        if not app_is_running():
            return True
    return not app_is_running()


def cleanup_old_backups(programs_root: Path) -> None:
    for backup in programs_root.glob(f"{APP_FOLDER}.old.*"):
        try:
            remove_directory_retry(backup, programs_root, attempts=2)
        except Exception as exc:
            log(f"Could not clean old backup {backup}: {exc}")


def archive_or_remove_existing_install(target_dir: Path, root: tk.Tk | None) -> None:
    programs_root = target_dir.parent
    if not target_dir.exists():
        return
    if not _path_inside(target_dir, programs_root) or target_dir.name != APP_FOLDER:
        raise RuntimeError(f"Unsicherer Installationspfad: {target_dir}")
    if not close_running_app(root):
        raise RuntimeError(
            "Die laufende Unified-OCR-App wurde nicht geschlossen. "
            "Bitte App beenden und Setup erneut starten."
        )

    try:
        remove_directory_retry(target_dir, programs_root)
        return
    except Exception as remove_error:
        log(f"Direct removal failed, trying archive rename: {remove_error}")

    backup = programs_root / f"{APP_FOLDER}.old.{time.strftime('%Y%m%d-%H%M%S')}"
    try:
        target_dir.rename(backup)
        log(f"Archived previous installation to {backup}")
    except OSError as exc:
        raise RuntimeError(
            "Die alte Installation konnte nicht entfernt oder zur Seite verschoben werden. "
            "Schliesse Unified OCR, warte einen Moment und starte das Setup erneut. "
            f"Pfad: {target_dir}. Windows-Fehler: {exc}"
        ) from exc


def safe_extract_zip(payload: Path, target_dir: Path) -> None:
    target_root = target_dir.resolve(strict=False)
    with zipfile.ZipFile(payload, "r") as archive:
        for member in archive.infolist():
            member_target = (target_dir / member.filename).resolve(strict=False)
            if not _path_inside(member_target, target_root):
                raise RuntimeError(f"Unsicherer ZIP-Eintrag: {member.filename}")
            archive.extract(member, target_dir)


def _common_command_dirs() -> list[Path]:
    dirs: list[Path] = []
    program_files = [Path(os.environ.get("ProgramFiles", r"C:\Program Files"))]
    if os.environ.get("ProgramFiles(x86)"):
        program_files.append(Path(os.environ["ProgramFiles(x86)"]))

    for root in program_files:
        dirs.append(root / "Tesseract-OCR")
        gs_root = root / "gs"
        if gs_root.exists():
            dirs.extend(sorted(gs_root.glob("gs*\\bin"), reverse=True))
        dirs.extend(sorted(root.glob("qpdf*\\bin"), reverse=True))
        qpdf_root = root / "qpdf"
        if qpdf_root.exists():
            dirs.extend(sorted(qpdf_root.glob("**\\bin"), reverse=True))

    local_appdata = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
    dirs.extend([
        local_appdata / "Microsoft" / "WindowsApps",
        local_appdata / "Programs" / "Ollama",
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Ollama",
    ])
    return [path for path in dirs if path.exists()]


def refresh_process_path() -> None:
    existing = os.environ.get("PATH", "")
    parts = existing.split(os.pathsep) if existing else []
    seen = {part.lower() for part in parts}
    additions = []
    for path in _common_command_dirs():
        value = str(path)
        if value.lower() not in seen:
            additions.append(value)
            seen.add(value.lower())
    if additions:
        os.environ["PATH"] = os.pathsep.join([*additions, existing])


def find_command(commands: tuple[str, ...]) -> str | None:
    refresh_process_path()
    for command in commands:
        found = shutil.which(command)
        if found:
            return found
    return None


def run_logged(command: list[str], progress: "ProgressWindow | None" = None) -> int:
    log(f"$ {' '.join(command)}")
    with log_path().open("a", encoding="utf-8") as handle:
        try:
            process = subprocess.Popen(
                command,
                stdout=handle,
                stderr=subprocess.STDOUT,
                creationflags=CREATE_NO_WINDOW,
            )
            while True:
                if progress is not None:
                    progress.window.update()
                    if progress.cancel_requested:
                        log("cancel requested")
                        try:
                            process.terminate()
                            process.wait(timeout=8)
                        except Exception:
                            process.kill()
                        log("exit=-2")
                        return -2
                return_code = process.poll()
                if return_code is not None:
                    log(f"exit={return_code}")
                    return int(return_code)
                time.sleep(0.25)
        except Exception as exc:
            log(f"failed: {exc}")
            return 1


def winget_available() -> bool:
    return bool(find_command(("winget", "winget.exe")))


class ProgressWindow:
    def __init__(self, root: tk.Tk, title: str, *, cancelable: bool = False):
        self.window = tk.Toplevel(root)
        self.window.title(title)
        self.window.geometry("560x150")
        self.window.resizable(False, False)
        self.window.protocol("WM_DELETE_WINDOW", lambda: None)
        self.cancel_requested = False
        self.label_var = tk.StringVar(value="Vorbereitung...")
        ttk.Label(self.window, textvariable=self.label_var, wraplength=520).pack(padx=24, pady=(24, 12), anchor="w")
        self.bar = ttk.Progressbar(self.window, mode="indeterminate")
        self.bar.pack(padx=24, pady=(0, 20), fill="x")
        if cancelable:
            ttk.Button(self.window, text="Abbrechen", command=self.cancel).pack(pady=(0, 12))
        self.bar.start(10)
        self.window.update()

    def set(self, text: str) -> None:
        self.label_var.set(text)
        self.window.update()

    def close(self) -> None:
        self.bar.stop()
        self.window.destroy()

    def cancel(self) -> None:
        self.cancel_requested = True
        self.set("Abbruch angefordert. Der laufende Prozess wird beendet...")


def install_required_dependencies(root: tk.Tk) -> bool:
    missing = [pkg for pkg in REQUIRED_PACKAGES if not find_command(pkg["commands"])]
    if not missing:
        return True

    labels = "\n".join(f"- {pkg['label']}" for pkg in missing)
    messagebox.showinfo(
        APP_NAME,
        "Fuer OCR werden noch Pflichtkomponenten installiert bzw. aktualisiert:\n\n"
        f"{labels}\n\n"
        "Dafuer wird WinGet genutzt. Je nach System kann Windows eine UAC- oder Installer-Abfrage anzeigen.",
        parent=root,
    )

    if not winget_available():
        messagebox.showerror(
            APP_NAME,
            "WinGet wurde nicht gefunden. Bitte installiere Windows App Installer/WinGet und danach:\n\n"
            "winget install -e --id UB-Mannheim.TesseractOCR\n"
            "winget install -e --id ArtifexSoftware.GhostScript\n"
            "winget install -e --id QPDF.QPDF",
            parent=root,
        )
        return False

    progress = ProgressWindow(root, "Unified OCR - Pflichtkomponenten")
    try:
        for pkg in missing:
            progress.set(f"Installiere {pkg['label']}...")
            code = run_logged([
                "winget",
                "install",
                "-e",
                "--id",
                pkg["winget_id"],
                "--accept-package-agreements",
                "--accept-source-agreements",
            ])
            if code not in (0, 3010):
                log(f"Installation failed for {pkg['label']} ({pkg['winget_id']})")
    finally:
        progress.close()

    refresh_process_path()
    still_missing = [pkg for pkg in REQUIRED_PACKAGES if not find_command(pkg["commands"])]
    if still_missing:
        labels = "\n".join(f"- {pkg['label']}" for pkg in still_missing)
        messagebox.showwarning(
            APP_NAME,
            "Einige Pflichtkomponenten wurden nach der Installation noch nicht gefunden:\n\n"
            f"{labels}\n\n"
            f"Installationslog:\n{log_path()}\n\n"
            "Falls die Installation gerade PATH-Variablen geaendert hat, starte Windows oder die App einmal neu.",
            parent=root,
        )
        return False
    return True


def choose_vram(root: tk.Tk) -> int | None:
    dialog = tk.Toplevel(root)
    dialog.title("Ollama-Modellauswahl")
    dialog.geometry("540x360")
    dialog.resizable(False, False)
    dialog.transient(root)
    dialog.grab_set()

    selected = tk.IntVar(value=8)
    result: dict[str, int | None] = {"value": None}
    ttk.Label(
        dialog,
        text=(
            "Wie viel VRAM hat deine Grafikkarte?\n\n"
            "Die App waehlt daraus lokale Ollama-Modelle fuer GLM-OCR, Vision, Text-Fusion und Analyse aus."
        ),
        wraplength=500,
    ).pack(padx=24, pady=(22, 12), anchor="w")

    for value in (8, 12, 16, 24, 32):
        rec = MODEL_RECOMMENDATIONS[value]
        ttk.Radiobutton(
            dialog,
            text=(
                f"{rec['label']}: Vision {rec['vision']}, Text {rec['fusion']} "
                f"(Download ca. {rec.get('estimated_download_gb', '?')} GB)"
            ),
            variable=selected,
            value=value,
        ).pack(padx=24, pady=3, anchor="w")

    button_frame = ttk.Frame(dialog)
    button_frame.pack(padx=24, pady=22, fill="x")

    def accept() -> None:
        result["value"] = selected.get()
        dialog.destroy()

    def cancel() -> None:
        result["value"] = None
        dialog.destroy()

    ttk.Button(button_frame, text="Empfehlung uebernehmen", command=accept).pack(side="right")
    ttk.Button(button_frame, text="Abbrechen", command=cancel).pack(side="right", padx=(0, 10))
    root.wait_window(dialog)
    return result["value"]


def unique_models(recommendation: dict) -> list[str]:
    ordered = [
        recommendation["glm_ocr"],
        recommendation["vision"],
        recommendation["fusion"],
        recommendation["analysis"],
    ]
    result = []
    seen = set()
    for model in ordered:
        if model not in seen:
            result.append(model)
            seen.add(model)
    return result


def _existing_parent(path: Path) -> Path:
    current = Path(path)
    while not current.exists() and current.parent != current:
        current = current.parent
    return current


def ollama_models_dir() -> Path:
    configured = os.environ.get("OLLAMA_MODELS")
    if configured:
        return Path(configured).expanduser()
    return Path(os.environ.get("USERPROFILE", str(Path.home()))) / ".ollama" / "models"


def free_space_gb(path: Path) -> float:
    anchor = _existing_parent(path)
    usage = shutil.disk_usage(anchor)
    return round(usage.free / (1024 ** 3), 1)


def confirm_model_disk_space(root: tk.Tk, recommendation: dict) -> bool:
    required = int(recommendation.get("required_free_gb", 0) or 0)
    if required <= 0:
        return True

    model_dir = ollama_models_dir()
    available = free_space_gb(model_dir)
    if available >= required:
        return True

    return messagebox.askyesno(
        APP_NAME,
        "Fuer den empfohlenen Ollama-Modell-Download ist wahrscheinlich zu wenig Speicher frei.\n\n"
        f"Ollama-Modellordner: {model_dir}\n"
        f"Frei: ca. {available} GB\n"
        f"Empfohlen: mindestens {required} GB\n\n"
        "Trotzdem fortfahren?",
        parent=root,
    )


def write_model_settings(recommendation: dict) -> None:
    data_dir = appdata_dir()
    data_dir.mkdir(parents=True, exist_ok=True)

    settings_path = data_dir / "settings.json"
    settings = {}
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
        except Exception:
            backup = settings_path.with_suffix(settings_path.suffix + ".broken")
            shutil.copy2(settings_path, backup)
            settings = {}

    models = settings.get("models") if isinstance(settings.get("models"), dict) else {}
    models.update({
        "glm_ocr": recommendation["glm_ocr"],
        "vision": recommendation["vision"],
        "fusion": recommendation["fusion"],
        "analysis": recommendation["analysis"],
    })
    settings["models"] = models
    settings.setdefault("unload_models_enabled", True)
    settings_path.write_text(json.dumps(settings, indent=4, ensure_ascii=False) + "\n", encoding="utf-8")

    llm_config_path = data_dir / "llm_config.yaml"
    if not llm_config_path.exists():
        llm_config_path.write_text(
            "\n".join([
                "providers:",
                "  ollama:",
                "    api_base: http://localhost:11434",
                "  openai:",
                "    api_key: ''",
                "  google:",
                "    api_key: ''",
                "  mistral:",
                "    api_key: ''",
                "stages:",
                f"  glm_ocr: ollama/{recommendation['glm_ocr']}",
                f"  vision: ollama/{recommendation['vision']}",
                f"  fusion: ollama/{recommendation['fusion']}",
                f"  analysis: ollama/{recommendation['analysis']}",
                "",
            ]),
            encoding="utf-8",
        )
    else:
        (data_dir / "ollama_model_recommendation.txt").write_text(
            "\n".join([
                recommendation["label"],
                recommendation["notes"],
                "",
                *[f"ollama pull {model}" for model in unique_models(recommendation)],
                "",
            ]),
            encoding="utf-8",
        )


def ollama_command() -> str | None:
    return find_command(("ollama",))


def ensure_ollama_available(root: tk.Tk) -> bool:
    if ollama_command():
        return True

    if not winget_available():
        messagebox.showerror(
            APP_NAME,
            "Ollama wurde nicht gefunden und WinGet ist nicht verfuegbar.\n\n"
            "Installiere Ollama manuell von https://ollama.com/download und starte danach die App.",
            parent=root,
        )
        return False

    progress = ProgressWindow(root, "Unified OCR - Ollama")
    try:
        progress.set("Installiere Ollama...")
        code = run_logged([
            "winget",
            "install",
            "-e",
            "--id",
            "Ollama.Ollama",
            "--accept-package-agreements",
            "--accept-source-agreements",
        ])
        if code not in (0, 3010):
            return False
    finally:
        progress.close()

    refresh_process_path()
    return bool(ollama_command())


def start_ollama_if_needed() -> None:
    command = ollama_command()
    if not command:
        return
    if run_logged([command, "list"]) == 0:
        return
    try:
        subprocess.Popen(
            [command, "serve"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
        )
        time.sleep(3)
    except Exception as exc:
        log(f"Could not start ollama serve: {exc}")


def pull_ollama_models(root: tk.Tk, recommendation: dict) -> bool:
    command = ollama_command()
    if not command:
        return False
    if not confirm_model_disk_space(root, recommendation):
        return False
    start_ollama_if_needed()
    models = unique_models(recommendation)
    progress = ProgressWindow(root, "Unified OCR - Ollama-Modelle", cancelable=True)
    failed = []
    cancelled = False
    try:
        for index, model in enumerate(models, 1):
            if progress.cancel_requested:
                cancelled = True
                break
            progress.set(f"Lade Modell {index}/{len(models)}:\n{model}")
            code = run_logged([command, "pull", model], progress=progress)
            if code == -2:
                cancelled = True
                break
            if code != 0:
                failed.append(model)
    finally:
        progress.close()

    if cancelled:
        messagebox.showinfo(
            APP_NAME,
            "Der Modell-Download wurde abgebrochen. Bereits fertig geladene Modelle bleiben in Ollama erhalten.",
            parent=root,
        )
        return False

    if failed:
        messagebox.showwarning(
            APP_NAME,
            "Einige Ollama-Modelle konnten nicht geladen werden:\n\n"
            + "\n".join(failed)
            + f"\n\nInstallationslog:\n{log_path()}",
            parent=root,
        )
        return False
    return True


def configure_ollama(root: tk.Tk) -> None:
    wants_ollama = messagebox.askyesno(
        APP_NAME,
        "Moechtest du Ollama fuer lokale KI-Modelle installieren bzw. verwenden?\n\n"
        "Nein bedeutet: Unified OCR funktioniert weiter mit klassischer OCR und optionalen API-Providern, "
        "lokale GLM-/Vision-/Fusion-Modelle werden aber nicht vorbereitet.",
        parent=root,
    )
    if not wants_ollama:
        return

    selected_vram = choose_vram(root)
    if not selected_vram:
        return

    recommendation = MODEL_RECOMMENDATIONS[selected_vram]
    write_model_settings(recommendation)

    if not ensure_ollama_available(root):
        messagebox.showwarning(
            APP_NAME,
            "Ollama konnte nicht automatisch installiert werden. Die Modellempfehlung wurde gespeichert, "
            "aber die Modelle muessen spaeter nachinstalliert werden.",
            parent=root,
        )
        return

    models = "\n".join(f"- {model}" for model in unique_models(recommendation))
    pull_now = messagebox.askyesno(
        APP_NAME,
        f"Empfohlene Modelle fuer {recommendation['label']}:\n\n"
        f"{models}\n\n"
        f"{recommendation['notes']}\n\n"
        f"Geschaetzter Download: ca. {recommendation.get('estimated_download_gb', '?')} GB\n"
        f"Empfohlen freier Speicher: ca. {recommendation.get('required_free_gb', '?')} GB\n\n"
        "Diese Downloads koennen sehr gross sein. Jetzt mit Ollama herunterladen?",
        parent=root,
    )
    if pull_now:
        pull_ollama_models(root, recommendation)


def shortcut(script_path: Path, target: Path, working_dir: Path, icon: Path) -> None:
    script_path.parent.mkdir(parents=True, exist_ok=True)
    ps = f"""
$Shell = New-Object -ComObject WScript.Shell
$Shortcut = $Shell.CreateShortcut('{script_path}')
$Shortcut.TargetPath = '{target}'
$Shortcut.WorkingDirectory = '{working_dir}'
$Shortcut.IconLocation = '{icon}'
$Shortcut.Save()
"""
    subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=CREATE_NO_WINDOW,
    )


def create_shortcuts(target_exe: Path) -> None:
    desktop = Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Desktop"
    start_menu = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
    shortcut(desktop / f"{APP_NAME}.lnk", target_exe, target_exe.parent, target_exe)
    shortcut(start_menu / f"{APP_NAME}.lnk", target_exe, target_exe.parent, target_exe)


def write_uninstaller(target_dir: Path) -> None:
    uninstall = target_dir / "Uninstall_UnifiedOCR.cmd"
    uninstall.write_text(
        "\r\n".join([
            "@echo off",
            "setlocal",
            "set INSTALL_DIR=%~dp0",
            "echo Removing Unified OCR shortcuts...",
            "del \"%USERPROFILE%\\Desktop\\Unified OCR.lnk\" 2>nul",
            "del \"%APPDATA%\\Microsoft\\Windows\\Start Menu\\Programs\\Unified OCR.lnk\" 2>nul",
            "echo Removing installed application files...",
            "cd /d \"%TEMP%\"",
            "rmdir /s /q \"%INSTALL_DIR%\"",
            "echo Unified OCR wurde entfernt.",
            "pause",
        ]),
        encoding="utf-8",
    )


def install(root: tk.Tk | None = None) -> Path:
    payload = resource_path(PAYLOAD_NAME)
    if not payload.exists():
        raise FileNotFoundError(f"Installer-Payload fehlt: {payload}")

    target_dir = install_dir()
    programs_root = target_dir.parent
    staging_dir = programs_root / f"{APP_FOLDER}.installing"
    programs_root.mkdir(parents=True, exist_ok=True)

    cleanup_old_backups(programs_root)
    remove_directory_retry(staging_dir, programs_root)
    staging_dir.mkdir(parents=True, exist_ok=True)

    try:
        safe_extract_zip(payload, staging_dir)

        staged_exe = staging_dir / EXE_NAME
        if not staged_exe.exists():
            raise FileNotFoundError(f"Installierte App wurde nicht gefunden: {staged_exe}")

        archive_or_remove_existing_install(target_dir, root)
        staging_dir.rename(target_dir)
    except Exception:
        try:
            remove_directory_retry(staging_dir, programs_root, attempts=2)
        except Exception as cleanup_error:
            log(f"Could not clean staging directory {staging_dir}: {cleanup_error}")
        raise

    target_exe = target_dir / EXE_NAME

    create_shortcuts(target_exe)
    write_uninstaller(target_dir)
    return target_exe


def installer_preflight(payload_override: str | None = None) -> dict:
    payload = Path(payload_override).resolve(strict=False) if payload_override else resource_path(PAYLOAD_NAME)
    target = install_dir()
    package_status = []
    for package in REQUIRED_PACKAGES:
        package_status.append({
            "label": package["label"],
            "winget_id": package["winget_id"],
            "found": bool(find_command(package["commands"])),
            "commands": list(package["commands"]),
        })
    payload_size = payload.stat().st_size if payload.exists() else 0
    model_dir = ollama_models_dir()
    recommendations = {
        str(vram): {
            "label": data["label"],
            "models": unique_models(data),
            "estimated_download_gb": data.get("estimated_download_gb", 0),
            "required_free_gb": data.get("required_free_gb", 0),
        }
        for vram, data in MODEL_RECOMMENDATIONS.items()
    }
    return {
        "app": APP_NAME,
        "payload": {
            "path": str(payload),
            "exists": payload.exists(),
            "size_mb": round(payload_size / (1024 ** 2), 2),
        },
        "target": {
            "install_dir": str(target),
            "exists": target.exists(),
            "parent_free_gb": free_space_gb(target.parent),
        },
        "dependencies": {
            "winget_available": winget_available(),
            "packages": package_status,
        },
        "ollama": {
            "installed": bool(ollama_command()),
            "models_dir": str(model_dir),
            "models_dir_free_gb": free_space_gb(model_dir),
            "recommendations": recommendations,
        },
    }


def maybe_run_preflight_cli() -> int | None:
    if "--preflight" not in sys.argv:
        return None
    payload = None
    if "--payload" in sys.argv:
        index = sys.argv.index("--payload")
        if index + 1 < len(sys.argv):
            payload = sys.argv[index + 1]
    result = installer_preflight(payload)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result["payload"]["exists"] else 2


def main() -> int:
    preflight_code = maybe_run_preflight_cli()
    if preflight_code is not None:
        return preflight_code

    root = tk.Tk()
    root.withdraw()
    try:
        target_exe = install(root)
    except Exception as exc:
        messagebox.showerror(APP_NAME, f"Installation fehlgeschlagen:\n\n{exc}", parent=root)
        root.destroy()
        return 1

    mandatory_ok = install_required_dependencies(root)
    configure_ollama(root)

    status_note = ""
    if not mandatory_ok:
        status_note = (
            "\n\nAchtung: Mindestens eine Pflichtkomponente wurde nicht sicher erkannt. "
            "Details stehen im Installationslog."
        )

    launch = messagebox.askyesno(
        APP_NAME,
        f"Unified OCR wurde installiert.\n\nInstallationsordner:\n{target_exe.parent}\n"
        f"Installationslog:\n{log_path()}"
        f"{status_note}\n\nJetzt starten?",
        parent=root,
    )
    root.destroy()
    if launch:
        subprocess.Popen([str(target_exe)], cwd=str(target_exe.parent))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
