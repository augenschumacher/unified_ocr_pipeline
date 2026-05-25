"""
core.cloud – Intelligente Dokumentenablage

Paket-Struktur:
    folder_registry.py  → Pfad-Registry (folder_registry.json)
    classifier.py       → LLM-Klassifikation
    organizer.py        → Dateien in Unterordner verschieben
"""
from .folder_registry import FolderRegistry
from .classifier import classify_document
from .organizer import DocumentOrganizer

__all__ = ["FolderRegistry", "classify_document", "DocumentOrganizer"]
