"""
core.llm – HTTP-Schicht + Pipeline-Tasks für Ollama

Paket-Struktur:
    ollama_client.py  → Reine HTTP/Streaming-Logik (OllamaClient)
    tasks.py          → Domain-Tasks (LLMClient extends OllamaClient)
"""
from .tasks import LLMClient

__all__ = ["LLMClient"]
