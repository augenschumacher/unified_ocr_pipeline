import os
import re
import threading
import customtkinter as ctk
from tkinter import messagebox
from pathlib import Path

from core.cloud.drive_sync import build_drive_sync_preview, sync_drive_folders
from core.cloud.folder_registry import FolderRegistry
from core.cloud.gdrive_client import GoogleDriveClient


INVALID_CHARS = set('<>:"/\\|?*')
RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
DEFAULT_TEMPLATE_CATEGORIES = [
    "Gesundheit",
    "Finanzen",
    "Versicherungen",
    "Steuern",
    "Arbeit",
    "Schule",
    "Wohnen",
    "Auto",
    "Verträge",
    "Sonstiges",
]


def get_paths_at_depth(tree: dict, target_depth: int, current_depth: int = 1, prefix: list = None) -> list[list[str]]:
    """Return all path component lists at a given depth."""
    if prefix is None:
        prefix = []

    results = []
    if current_depth == target_depth:
        for key in tree.keys():
            results.append(prefix + [key])
        return results

    for key, val in tree.items():
        if isinstance(val, dict):
            results.extend(get_paths_at_depth(val, target_depth, current_depth + 1, prefix + [key]))
    return results


def get_node_by_path(tree: dict, path: list[str]) -> dict:
    current = tree
    for part in path:
        current = current[part]
    return current


def normalize_folder_name(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip())


def validate_folder_name(name: str) -> tuple[bool, str]:
    raw = name or ""
    if raw != raw.strip():
        return False, "Der Name darf nicht mit Leerzeichen beginnen oder enden."
    cleaned = normalize_folder_name(raw)
    if not cleaned:
        return False, "Der Name darf nicht leer sein."
    if cleaned != raw or cleaned.endswith("."):
        return False, "Der Name darf nicht mit Punkt enden und keine mehrfachen Leerzeichen enthalten."
    if any(ord(ch) < 32 for ch in cleaned):
        return False, "Der Name darf keine Steuerzeichen enthalten."
    found = sorted({ch for ch in cleaned if ch in INVALID_CHARS})
    if found:
        return False, f"Der Name enthält ungültige Zeichen: {' '.join(found)}"
    if cleaned.upper() in RESERVED_NAMES:
        return False, f"'{cleaned}' ist unter Windows ein reservierter Name."
    return True, ""


def find_case_insensitive_key(node: dict, name: str) -> str | None:
    folded = name.casefold()
    return next((key for key in node.keys() if key.casefold() == folded), None)


def add_child_node(node: dict, name: str) -> tuple[bool, str]:
    cleaned = normalize_folder_name(name)
    ok, message = validate_folder_name(cleaned)
    if not ok:
        return False, message
    existing = find_case_insensitive_key(node, cleaned)
    if existing:
        return False, f"Der Ordner '{existing}' existiert bereits auf dieser Ebene."
    node[cleaned] = {}
    return True, cleaned


def apply_standard_template(tree: dict, primary_paths: list[str]) -> tuple[int, list[str]]:
    """Apply default categories below each person/primary path."""
    added = 0
    rejected = []
    for raw_name in primary_paths:
        name = normalize_folder_name(raw_name)
        ok, message = validate_folder_name(name)
        if not ok:
            rejected.append(f"{raw_name}: {message}")
            continue

        existing = find_case_insensitive_key(tree, name)
        if existing:
            person_key = existing
        else:
            tree[name] = {}
            person_key = name
            added += 1

        for category in DEFAULT_TEMPLATE_CATEGORIES:
            if not find_case_insensitive_key(tree[person_key], category):
                tree[person_key][category] = {}
                added += 1
    return added, rejected


def iter_tree_paths(tree: dict, prefix: tuple[str, ...] = ()):
    for key, value in tree.items():
        current = (*prefix, key)
        yield current
        if isinstance(value, dict):
            yield from iter_tree_paths(value, current)


def create_final_directories(base_dir: Path, tree: dict) -> list[Path]:
    final_dir = Path(base_dir) / "final"
    created = []
    for parts in iter_tree_paths(tree):
        target = final_dir.joinpath(*parts)
        target.mkdir(parents=True, exist_ok=True)
        created.append(target)
    return created


class TemplateDialog(ctk.CTkToplevel):
    def __init__(self, parent, existing_names: list[str], on_apply):
        super().__init__(parent)
        self.on_apply = on_apply
        self.title("Standardvorlage")
        self.geometry("560x220")
        self.minsize(520, 210)
        self.transient(parent)
        self.grab_set()
        self.after(100, self.focus_force)

        default_names = ", ".join(existing_names) if existing_names else os.environ.get("USERNAME", "Meine Person")

        frame = ctk.CTkFrame(self)
        frame.pack(fill="both", expand=True, padx=18, pady=18)
        frame.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            frame,
            text="Personen als Primärpfad",
            font=ctk.CTkFont(size=16, weight="bold"),
        ).grid(row=0, column=0, sticky="w", pady=(0, 8))

        ctk.CTkLabel(
            frame,
            text="Gib die Personen/Hauptpfade kommagetrennt ein. Darunter werden Standard-Kategorien angelegt.",
            text_color="gray",
        ).grid(row=1, column=0, sticky="w", pady=(0, 10))

        self.entry = ctk.CTkEntry(frame)
        self.entry.grid(row=2, column=0, sticky="ew", pady=(0, 16))
        self.entry.insert(0, default_names)

        button_row = ctk.CTkFrame(frame, fg_color="transparent")
        button_row.grid(row=3, column=0, sticky="ew")
        ctk.CTkButton(button_row, text="Abbrechen", width=120, command=self.destroy).pack(side="left")
        ctk.CTkButton(
            button_row,
            text="Vorlage anwenden",
            width=160,
            command=self.apply,
        ).pack(side="right")

    def apply(self):
        names = [part.strip() for part in self.entry.get().split(",") if part.strip()]
        if not names:
            messagebox.showerror("Fehler", "Bitte gib mindestens eine Person ein.")
            return
        self.on_apply(names)
        self.destroy()


class PathManagerWindow(ctk.CTkToplevel):
    def __init__(
        self,
        parent,
        base_dir: Path,
        on_save_callback=None,
        onboarding: bool = False,
        gdrive_token_path: str | None = None,
    ):
        super().__init__(parent)
        self.base_dir = Path(base_dir)
        self.on_save_callback = on_save_callback
        self.onboarding = onboarding
        self.gdrive_token_path = gdrive_token_path

        self.registry = FolderRegistry(self.base_dir)
        self.tree = self.registry.get_tree()

        self.title("Pfad-Konfiguration & Setup-Wizard")
        self.geometry("900x590")
        self.minsize(820, 520)
        self.transient(parent)
        self.after(100, self.lift)
        self.after(200, self.focus_force)
        self.grab_set()

        self.levels = ["Primärpfad", "Kategorie", "Unterkategorie"]
        self.current_level_idx = 0

        max_depth = self._get_tree_max_depth(self.tree)
        while len(self.levels) < max_depth:
            self.levels.append(f"Pfadstufe {len(self.levels) + 1}")

        self.selected_parent_path = []
        self.setup_ui()

    def _get_tree_max_depth(self, tree: dict) -> int:
        if not tree:
            return 0
        depths = []
        for _, value in tree.items():
            if isinstance(value, dict):
                depths.append(1 + self._get_tree_max_depth(value))
            else:
                depths.append(1)
        return max(depths) if depths else 0

    def setup_ui(self):
        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1)

        self.color_active = ("#2f6fed", "#4c8dff")
        self.color_inactive = ("#e5e5ea", "#2d2d34")
        self.color_bg = ("#f4f4f9", "#121214")
        self.color_card = ("#ffffff", "#1e1e24")
        self.color_card_item = ("#f0f0f5", "#1a1a1f")
        self.color_accent = ("#06d6a0", "#06d6a0")
        self.color_danger = ("#d90429", "#d90429")
        self.color_del_bg = ("#ffe5ec", "#3a0913")
        self.color_del_fg = ("#d90429", "#ff4d6d")
        self.color_cancel_bg = ("#d1d1d6", "#2b2d42")
        self.color_cancel_hover = ("#a7a7a8", "#45475a")
        self.color_cancel_text = ("#333333", "white")
        self.color_tab_text = ("white", "white")
        self.color_tab_inactive_text = ("#5c5c5c", "#d1d1d6")
        self.color_parent_selected = ("#2f6fed", "#2357c7")
        self.color_parent_selected_hover = ("#4c8dff", "#366bd8")
        self.color_parent_unselected_hover = ("#e5e5ea", "#2c2c35")
        self.color_parent_text = ("black", "white")
        self.color_parent_selected_text = ("white", "white")
        self.color_breadcrumbs = ("#5c5c5c", "#8d99ae")

        self.configure(fg_color=self.color_bg)

        self.header_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.header_frame.grid(row=0, column=0, padx=20, pady=(15, 10), sticky="ew")
        self.header_frame.grid_columnconfigure(0, weight=1)

        title = "Ersteinrichtung: Ablagepfade" if self.onboarding else "Pfad-Konfiguration"
        self.title_label = ctk.CTkLabel(
            self.header_frame,
            text=title,
            font=ctk.CTkFont(size=20, weight="bold"),
        )
        self.title_label.grid(row=0, column=0, sticky="w", pady=(0, 6))

        self.subtitle_label = ctk.CTkLabel(
            self.header_frame,
            text="Primärpfade sind Personen oder Hauptbereiche. Darunter liegen Kategorien wie Gesundheit, Finanzen oder Schule.",
            text_color="gray",
        )
        self.subtitle_label.grid(row=1, column=0, sticky="w", pady=(0, 10))

        self.tabs_container = ctk.CTkFrame(self.header_frame, fg_color="transparent")
        self.tabs_container.grid(row=2, column=0, sticky="ew")
        self.tab_buttons_frame = ctk.CTkFrame(self.tabs_container, fg_color="transparent")
        self.tab_buttons_frame.pack(side="left")
        self.draw_tabs()

        self.content_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.content_frame.grid(row=1, column=0, padx=20, pady=10, sticky="nsew")
        self.content_frame.grid_columnconfigure(0, weight=1)
        self.content_frame.grid_columnconfigure(1, weight=2)
        self.content_frame.grid_rowconfigure(0, weight=1)

        self.left_panel = ctk.CTkFrame(self.content_frame, fg_color=self.color_card)
        self.left_label = ctk.CTkLabel(
            self.left_panel,
            text="Eltern-Pfad auswählen:",
            font=ctk.CTkFont(size=14, weight="bold"),
        )
        self.left_label.pack(anchor="w", padx=15, pady=(15, 5))
        self.left_scroll = ctk.CTkScrollableFrame(self.left_panel, fg_color="transparent")
        self.left_scroll.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        self.right_panel = ctk.CTkFrame(self.content_frame, fg_color=self.color_card)
        self.right_panel.grid(row=0, column=1, padx=(10, 0), sticky="nsew")
        self.right_panel.grid_rowconfigure(1, weight=1)
        self.right_panel.grid_columnconfigure(0, weight=1)

        self.right_header_frame = ctk.CTkFrame(self.right_panel, fg_color="transparent")
        self.right_header_frame.pack(fill="x", padx=15, pady=(15, 5))
        self.right_label = ctk.CTkLabel(
            self.right_header_frame,
            text="Verzeichnisse verwalten:",
            font=ctk.CTkFont(size=14, weight="bold"),
        )
        self.right_label.pack(side="left")
        self.breadcrumbs_label = ctk.CTkLabel(
            self.right_header_frame,
            text="",
            font=ctk.CTkFont(size=12, slant="italic"),
            text_color=self.color_breadcrumbs,
        )
        self.breadcrumbs_label.pack(side="right", padx=10)

        self.right_scroll = ctk.CTkScrollableFrame(self.right_panel, fg_color="transparent")
        self.right_scroll.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        self.input_area = ctk.CTkFrame(self.right_panel, fg_color="transparent")
        self.input_area.pack(fill="x", padx=15, pady=(0, 15))
        self.new_entry = ctk.CTkEntry(self.input_area, placeholder_text="Neuen Ordner eingeben...", height=35)
        self.new_entry.pack(side="left", fill="x", expand=True, padx=(0, 10))
        self.new_entry.bind("<Return>", lambda e: self.add_new_item())
        self.add_btn = ctk.CTkButton(
            self.input_area,
            text="Hinzufügen",
            width=100,
            height=35,
            fg_color=self.color_active,
            command=self.add_new_item,
        )
        self.add_btn.pack(side="right")

        self.footer_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.footer_frame.grid(row=2, column=0, padx=20, pady=15, sticky="ew")
        self.footer_frame.grid_columnconfigure(1, weight=1)

        self.template_btn = ctk.CTkButton(
            self.footer_frame,
            text="Standardvorlage...",
            width=150,
            command=self.open_template_dialog,
        )
        self.template_btn.grid(row=0, column=0, padx=(0, 10), sticky="w")

        self.drive_sync_btn = ctk.CTkButton(
            self.footer_frame,
            text="Google Drive sync...",
            width=155,
            command=self.sync_google_drive,
        )
        self.drive_sync_btn.grid(row=0, column=1, padx=(0, 10), sticky="w")

        self.cancel_btn = ctk.CTkButton(
            self.footer_frame,
            text="Später",
            width=110,
            fg_color=self.color_cancel_bg,
            hover_color=self.color_cancel_hover,
            text_color=self.color_cancel_text,
            command=self.destroy,
        )
        self.cancel_btn.grid(row=0, column=2, padx=(10, 8), sticky="e")

        self.save_btn = ctk.CTkButton(
            self.footer_frame,
            text="Konfiguration abschließen",
            width=210,
            fg_color=self.color_accent,
            hover_color=("#05c490", "#05c490"),
            text_color="#121214",
            font=ctk.CTkFont(weight="bold"),
            command=self.save_and_close,
        )
        self.save_btn.grid(row=0, column=3, sticky="e")

        self.select_level(0)

    def draw_tabs(self):
        for widget in self.tab_buttons_frame.winfo_children():
            widget.destroy()

        for i, level_name in enumerate(self.levels):
            is_active = (i == self.current_level_idx)
            btn = ctk.CTkButton(
                self.tab_buttons_frame,
                text=level_name,
                fg_color=self.color_active if is_active else self.color_inactive,
                hover_color=("#4c8dff", "#366bd8") if is_active else ("#d1d1d6", "#3e3e4a"),
                text_color=self.color_tab_text if is_active else self.color_tab_inactive_text,
                font=ctk.CTkFont(weight="bold" if is_active else "normal"),
                width=120,
                height=35,
                command=lambda idx=i: self.select_level(idx),
            )
            btn.pack(side="left", padx=(0, 5))

        self.plus_btn = ctk.CTkButton(
            self.tabs_container,
            text="+",
            width=35,
            height=35,
            fg_color=self.color_inactive,
            hover_color=("#d1d1d6", "#3e3e4a"),
            text_color=self.color_tab_inactive_text,
            font=ctk.CTkFont(size=16, weight="bold"),
            command=self.add_level_stage,
        )
        self.plus_btn.pack(side="left", padx=5)

    def add_level_stage(self):
        new_stage_idx = len(self.levels) + 1
        self.levels.append(f"Pfadstufe {new_stage_idx}")
        self.draw_tabs()
        self.select_level(len(self.levels) - 1)

    def update_panels_layout(self):
        if self.current_level_idx == 0:
            self.left_panel.grid_forget()
            self.right_panel.grid(row=0, column=0, columnspan=2, padx=0, sticky="nsew")
        else:
            self.left_panel.grid(row=0, column=0, padx=(0, 10), sticky="nsew")
            self.right_panel.grid(row=0, column=1, padx=(10, 0), sticky="nsew")

    def select_level(self, idx):
        if idx > 0:
            parent_paths = get_paths_at_depth(self.tree, idx)
            if not parent_paths:
                messagebox.showerror(
                    "Konfiguration erforderlich",
                    f"Bitte legen Sie zuerst mindestens einen Pfad für '{self.levels[idx - 1]}' an.",
                )
                return

        self.current_level_idx = idx
        self.draw_tabs()
        self.update_panels_layout()

        if idx > 0:
            parent_paths = get_paths_at_depth(self.tree, idx)
            self.selected_parent_path = parent_paths[0]
            self.populate_left_panel(parent_paths)
        else:
            self.selected_parent_path = []

        self.populate_right_panel()

    def populate_left_panel(self, parent_paths):
        for widget in self.left_scroll.winfo_children():
            widget.destroy()

        for path in parent_paths:
            path_str = " > ".join(path)
            is_selected = path == self.selected_parent_path
            btn = ctk.CTkButton(
                self.left_scroll,
                text=path_str,
                anchor="w",
                fg_color=self.color_parent_selected if is_selected else "transparent",
                hover_color=self.color_parent_selected_hover if is_selected else self.color_parent_unselected_hover,
                text_color=self.color_parent_selected_text if is_selected else self.color_parent_text,
                height=30,
                command=lambda p=path: self.select_parent(p),
            )
            btn.pack(fill="x", pady=2)

    def select_parent(self, path):
        self.selected_parent_path = path
        parent_paths = get_paths_at_depth(self.tree, self.current_level_idx)
        self.populate_left_panel(parent_paths)
        self.populate_right_panel()

    def populate_right_panel(self):
        for widget in self.right_scroll.winfo_children():
            widget.destroy()

        if self.selected_parent_path:
            self.breadcrumbs_label.configure(text=f"Pfad: {' > '.join(self.selected_parent_path)}")
        else:
            self.breadcrumbs_label.configure(text="Ebene: Primärpfad")

        try:
            node = get_node_by_path(self.tree, self.selected_parent_path) if self.selected_parent_path else self.tree
            children = sorted(node.keys(), key=str.casefold)
        except Exception:
            children = []

        for child in children:
            row_frame = ctk.CTkFrame(self.right_scroll, fg_color="transparent")
            row_frame.pack(fill="x", pady=3)

            card_frame = ctk.CTkFrame(row_frame, fg_color=self.color_card_item, height=35)
            card_frame.pack(side="left", fill="x", expand=True, padx=(0, 5))
            card_frame.pack_propagate(False)

            lbl = ctk.CTkLabel(card_frame, text=child, font=ctk.CTkFont(size=13), anchor="w")
            lbl.pack(fill="both", expand=True, padx=10)

            del_btn = ctk.CTkButton(
                row_frame,
                text="X",
                width=35,
                height=35,
                fg_color=self.color_del_bg,
                hover_color=self.color_danger,
                text_color=self.color_del_fg,
                font=ctk.CTkFont(weight="bold"),
                command=lambda c=child: self.delete_item(c),
            )
            del_btn.pack(side="right")

        try:
            self.right_scroll._parent_canvas.yview_moveto(1.0)
        except Exception:
            pass

    def delete_item(self, child):
        node = get_node_by_path(self.tree, self.selected_parent_path) if self.selected_parent_path else self.tree
        has_children = len(node.get(child, {})) > 0
        message = (
            f"'{child}' wird aus der Ablageliste entfernt. Bereits erstellte Ordner auf der Festplatte bleiben erhalten."
        )
        if has_children:
            message += "\n\nDer Eintrag enthält Unterordner; diese werden ebenfalls nur aus der Liste entfernt."
        if not messagebox.askyesno("Eintrag entfernen", message):
            return

        if child in node:
            del node[child]
        self.populate_right_panel()

    def add_new_item(self):
        val = normalize_folder_name(self.new_entry.get())
        if self.selected_parent_path:
            node = get_node_by_path(self.tree, self.selected_parent_path)
        else:
            node = self.tree

        ok, result = add_child_node(node, val)
        if not ok:
            messagebox.showerror("Fehler", result)
            return

        self.new_entry.delete(0, "end")
        self.new_entry.focus()
        self.populate_right_panel()

    def open_template_dialog(self):
        TemplateDialog(self, list(self.tree.keys()), self.apply_template)

    def apply_template(self, primary_paths: list[str]):
        added, rejected = apply_standard_template(self.tree, primary_paths)
        self.select_level(0)
        if rejected:
            messagebox.showwarning("Teilweise angewendet", "\n".join(rejected))
        else:
            messagebox.showinfo("Vorlage angewendet", f"{added} Einträge wurden ergänzt.")

    def sync_google_drive(self):
        if not self.gdrive_token_path:
            messagebox.showwarning(
                "Google Drive nicht aktiv",
                "Bitte Google Drive zuerst in den Einstellungen verknüpfen und aktivieren.",
            )
            return

        ok, message = self._validate_tree()
        if not ok:
            messagebox.showerror("Konfiguration ungültig", message)
            return

        try:
            self.registry.save_tree(self.tree)
            preview = build_drive_sync_preview(self.registry)
        except Exception as e:
            messagebox.showerror("Fehler", f"Die Ablagestruktur konnte nicht vorbereitet werden:\n{e}")
            return

        client = GoogleDriveClient()
        if not client.is_authenticated(self.gdrive_token_path):
            messagebox.showwarning(
                "Nicht verknüpft",
                "Google Drive ist nicht authentifiziert. Bitte zuerst in den Einstellungen verknüpfen.",
            )
            return

        missing = preview["missing_ids"]
        lines = [
            f"{preview['total']} Ablagepfade werden in Google Drive geprüft.",
            f"{len(missing)} Pfade haben noch keine gespeicherte Drive-ID.",
        ]
        if missing:
            shown = "\n".join(f"- {path}" for path in missing[:12])
            lines.append("\nWird geprüft und bei Bedarf angelegt:\n" + shown)
            if len(missing) > 12:
                lines.append(f"... und {len(missing) - 12} weitere")
        lines.append("\nEs werden keine Drive-Ordner gelöscht.")

        if not messagebox.askyesno("Google Drive synchronisieren", "\n".join(lines)):
            return

        self.drive_sync_btn.configure(state="disabled", text="Synchronisiere...")

        def worker():
            try:
                result = sync_drive_folders(self.base_dir, self.gdrive_token_path, client=client)
                self.after(0, self._on_drive_sync_done, result, None)
            except Exception as exc:
                self.after(0, self._on_drive_sync_done, None, exc)

        threading.Thread(target=worker, daemon=True).start()

    def _on_drive_sync_done(self, result, error):
        self.drive_sync_btn.configure(state="normal", text="Google Drive sync...")
        self.registry = FolderRegistry(self.base_dir)
        self.tree = self.registry.get_tree()
        self.populate_right_panel()
        if error:
            messagebox.showerror("Google Drive Sync fehlgeschlagen", str(error))
            return

        conflicts = result.get("conflicts", [])
        message = (
            f"Google Drive Sync abgeschlossen.\n\n"
            f"Neu erstellt: {len(result.get('created', []))}\n"
            f"Gefunden/verknüpft: {len(result.get('found', []))}\n"
            f"Konflikte: {len(conflicts)}"
        )
        if conflicts:
            first = conflicts[0].get("message", "Konflikt in Google Drive")
            message += f"\n\nHinweis: {first}"
        messagebox.showinfo("Google Drive synchronisiert", message)

    def _validate_tree(self) -> tuple[bool, str]:
        if not self.tree:
            return False, "Bitte legen Sie mindestens einen Primärpfad an."

        for parts in iter_tree_paths(self.tree):
            for part in parts:
                ok, message = validate_folder_name(part)
                if not ok:
                    return False, f"Ungültiger Pfad {'/'.join(parts)}: {message}"
        return True, ""

    def save_and_close(self):
        ok, message = self._validate_tree()
        if not ok:
            messagebox.showerror("Konfiguration ungültig", message)
            return

        try:
            self.registry.save_tree(self.tree)
            created = create_final_directories(self.base_dir, self.tree)
            if self.on_save_callback:
                self.on_save_callback(created)
            self.destroy()
        except Exception as e:
            messagebox.showerror("Fehler", f"Konnte Konfiguration nicht speichern: {e}")
