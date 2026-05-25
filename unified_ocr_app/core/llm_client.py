# Compatibility-Shim: Weiterleitungen auf das neue core.llm Paket
# Diese Datei kann gelöscht werden sobald alle externen Referenzen aktualisiert sind.
from core.llm import LLMClient
from core.llm.ollama_client import OllamaClient

__all__ = ["LLMClient", "OllamaClient"]
