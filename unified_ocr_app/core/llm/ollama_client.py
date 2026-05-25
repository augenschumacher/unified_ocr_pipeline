"""
ollama_client.py – LiteLLM und SQLite Caching-Schicht für Unified OCR

Verantwortlichkeiten:
    - Normalisierung des Model-Providers (z.B. ollama/ openai/ google/ mistral/)
    - Lokaler SQLite-Cache (MD5 raw_text + model + prompt_version)
    - LiteLLM integration für universelle Provider-Kompatibilität
    - Streaming-Response-Loop für GUI Thinking- & Output-Panels
    - Retry-Logik und GPU-Serialisierung (nur bei lokalem Ollama)
"""

import json
import re
import base64
import time
import threading
import logging
import hashlib
from pathlib import Path

# LiteLLM importieren und Telemetrie/interne Warnungen deaktivieren
try:
    # Logger-Levels VOR dem Import konfigurieren, um Modul-Lade-Warnungen zu unterdrücken
    logging.getLogger("LiteLLM").setLevel(logging.ERROR)
    logging.getLogger("litellm").setLevel(logging.ERROR)
    import litellm
    litellm.telemetry = False
except ImportError:
    litellm = None

logger = logging.getLogger("UnifiedOCR")

_OLLAMA_LOCK = threading.Lock()

class DummyLock:
    def __enter__(self):
        return self
    def __exit__(self, exc_type, exc_val, exc_tb):
        pass

_DUMMY_LOCK = DummyLock()


class OllamaClient:
    """
    Basis-Klasse für die LLM-Kommunikation über LiteLLM.
    Unterstützt Caching (SQLite), provider-übergreifendes Routing und GUI-Streaming.
    """

    def __init__(self, prompts: dict = None, log_callback=None, keep_alive: str = "15m", config_path=None):
        self.prompts         = prompts or {}
        self.log_callback    = log_callback
        self.stream_callback = None   # Wird von der GUI gesetzt
        self.keep_alive      = keep_alive
        self.force_pipeline  = False  # Globaler Pipeline-Erzwingungsschalter (Cache-Bypass)

        # SQLite Caching initialisieren
        from core.cache import SQLiteCache
        from core.runtime_paths import get_user_data_dir
        self.cache = SQLiteCache(get_user_data_dir() / "cache.db")

        # Zentrale LLM YAML-Konfiguration laden
        from .config import load_llm_config
        self.llm_config = load_llm_config(config_path)

    # ------------------------------------------------------------------ #
    #  Interne Hilfsmethoden                                               #
    # ------------------------------------------------------------------ #

    def _log(self, msg: str):
        if self.log_callback:
            self.log_callback(msg)

    def _get_prompt(self, key: str, default: str) -> str:
        """Benutzerdefinierter Prompt aus settings.json, sonst der Default."""
        return self.prompts.get(key, "").strip() or default

    def _encode_image(self, image_path: str) -> str:
        """Liest eine Bilddatei und gibt Base64-kodierte Bytes zurück."""
        with open(image_path, "rb") as f:
            return base64.b64encode(f.read()).decode("utf-8")

    def _detect_suffix_repetition(self, text: str, min_pattern_len: int = 6, min_total_len: int = 180) -> bool:
        """
        Prüft, ob das Ende des Textes aus einer sich wiederholenden Sequenz besteht.
        Verhindert unendliche LLM-Wiederholungsschleifen.
        """
        if len(text) < min_total_len:
            return False

        suffix = text[-min_total_len*2:]

        for l in range(min_pattern_len, min_total_len // 2 + 1):
            pattern = suffix[-l:]

            if not pattern.strip() or len(set(pattern)) <= 2:
                continue

            repeats = 0
            idx = len(suffix)
            while idx >= l:
                if suffix[idx-l:idx] == pattern:
                    repeats += 1
                    idx -= l
                else:
                    break

            if repeats * l >= min_total_len:
                return True

        return False

    def _build_payload(
        self,
        model:         str,
        system_prompt: str,
        user_prompt:   str,
        image_path:    str  = None,
        think:         bool = False,
    ) -> dict:
        """
        Hilfsmethode für Kompatibilität mit Tests.
        Erstellt die JSON-Payload im Stil des alten Ollama-Clients.
        """
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            "stream": True,
            "keep_alive": getattr(self, "keep_alive", "15m")
        }
        
        # 'think'-Parameter Strategie (reine OCR-Modelle bekommen ihn nicht)
        is_no_think = any(x in model.lower() for x in ("glm-ocr",))
        if not is_no_think:
            payload["think"] = think
            
        return payload

    def _process_stream(self, response_generator, think_enabled: bool = False, max_tokens: int = 4096) -> str:
        """
        Verarbeitet den LiteLLM-Streaming-Response Token für Token.
        Unterstützt native thinking_content (reasoning_content) und Inline-Tags (<think>).
        """
        result      = []
        token_count = 0
        accumulated = ""
        output_sent = ""
        thinking_accumulated = ""

        thinking_sent = ""
        native_buf    = ""
        native_sent   = ""

        # Fallback für Test-Kompatibilität (wenn response_generator iter_lines besitzt)
        if hasattr(response_generator, "iter_lines"):
            iterator = response_generator.iter_lines()
        else:
            iterator = response_generator

        for chunk in iterator:
            try:
                token = ""
                native_chunk = ""

                # Falls der Chunk aus Bytes besteht (z. B. in Tests), decodieren und laden
                if isinstance(chunk, bytes):
                    try:
                        chunk = json.loads(chunk.decode("utf-8"))
                    except Exception:
                        pass

                # Token und reasoning_content / thinking aus Chunk extrahieren
                if hasattr(chunk, "choices") and chunk.choices:
                    delta = chunk.choices[0].delta
                    if hasattr(delta, "content") and delta.content:
                        token = delta.content
                    if hasattr(delta, "reasoning_content") and delta.reasoning_content:
                        native_chunk = delta.reasoning_content
                elif isinstance(chunk, dict):
                    if "choices" in chunk:
                        choices = chunk.get("choices", [])
                        if choices:
                            delta = choices[0].get("delta", {})
                            token = delta.get("content", "")
                            native_chunk = delta.get("reasoning_content", "")
                    else:
                        # Unterstützung für altes Ollama-Format (z. B. in Tests)
                        msg = chunk.get("message", {})
                        token = msg.get("content", "")
                        native_chunk = msg.get("thinking", "")

                # ── Variante A: Native reasoning_content / thinking field ──
                if native_chunk:
                    thinking_accumulated += native_chunk
                    if think_enabled:
                        if not native_buf:
                            self._log("    🧠 [Debug] Thinking AKTIV – reasoning_content empfangen, wird angezeigt.")
                        native_buf += native_chunk
                        delta_str = native_buf[len(native_sent):]
                        if delta_str and self.stream_callback:
                            self.stream_callback(delta_str, True)
                        native_sent = native_buf
                    else:
                        if not native_buf:
                            self._log("    ⚠️ [Debug] Thinking AUS – Modell sendet trotzdem reasoning_content! Werden IGNORIERT.")
                            native_buf = "ignored"

                # ── Token-Zähler & Sicherheitslimit ──────────────────────
                if token or native_chunk:
                    token_count += 1
                    if token_count % 50 == 0:
                        self._log(f"    → {token_count} Token empfangen...")

                if token_count > max_tokens:
                    self._log(f"    🚨 [Warnung] Sicherheitslimit von {max_tokens} Tokens überschritten! Breche ab, um Dauerschleife zu verhindern.")
                    break

                # ── Wiederholungsschleifen-Erkennung ──────────────────────
                if self._detect_suffix_repetition(accumulated) or self._detect_suffix_repetition(thinking_accumulated):
                    self._log("    🚨 [Warnung] Wiederholungsschleife im LLM-Stream erkannt! Breche Generierung ab.")
                    break

                # ── Content-Token verarbeiten ──────────────────────────────
                if token:
                    result.append(token)
                    accumulated += token

                    current_thinking = ""
                    current_output   = ""

                    # ── Variante B: <think>-Tags im Content-Stream ─────────
                    if "<think>" in accumulated:
                        pre, post = accumulated.split("<think>", 1)
                        if "</think>" in post:
                            think_body, rest = post.split("</think>", 1)
                            current_thinking = think_body
                            current_output   = pre + rest
                        else:
                            current_thinking = post
                            current_output   = pre
                    elif len(accumulated) < 8 and "<think>".startswith(accumulated):
                        pass
                    else:
                        current_output = accumulated

                    # Thinking-Inkrement senden
                    if think_enabled and current_thinking and not native_sent:
                        delta_t = current_thinking[len(thinking_sent):]
                        if delta_t and self.stream_callback:
                            self.stream_callback(delta_t, True)
                        thinking_sent = current_thinking

                    # Output-Inkrement senden
                    if current_output:
                        delta_o = current_output[len(output_sent):]
                        if delta_o and self.stream_callback:
                            self.stream_callback(delta_o, False)
                        output_sent = current_output

            except Exception as e:
                logger.error(f"Fehler bei Stream-Token-Verarbeitung: {e}")
                pass

        full = "".join(result)
        return re.sub(r"<think>.*?</think>", "", full, flags=re.DOTALL).strip()

    # ------------------------------------------------------------------ #
    #  Öffentliche Query-Methode                                           #
    # ------------------------------------------------------------------ #

    def query(
        self,
        model:         str,
        system_prompt: str,
        user_prompt:   str,
        image_path:    str  = None,
        think:         bool = False,
        max_tokens:    int  = 4096,
        raw_text:      str  = None,
        prompt_version: str = None
    ) -> str:
        """
        Sendet eine Chat-Anfrage via LiteLLM (Streaming, bis zu 3 Versuche).
        Sucht zuerst im lokalen SQLite-Cache, außer force_pipeline ist True.
        """
        if not model or model == "Keins":
            self._log(f"    [Skip] Modell '{model}' ist deaktiviert.")
            return ""

        # Model-Name normalisieren: Standardmäßig Ollama, falls kein Provider angegeben
        if "/" not in model:
            model = f"ollama/{model}"

        # ── LOGISCHES SEITEN-CACHING ───────────────────────────────────────
        # Schlüssel generieren aus raw_text, Modellname und Prompt-Version
        raw_text_content = raw_text if raw_text is not None else user_prompt
        p_ver = str(prompt_version if prompt_version is not None else getattr(self, "prompt_version", 1))

        # Falls force_pipeline = False, Cache prüfen
        if not getattr(self, "force_pipeline", False):
            cached_res = self.cache.get(raw_text_content, model, p_ver)
            if cached_res is not None:
                self._log(f"    [Cache HIT] Verwende gecachtes Ergebnis für Modell: {model}")
                if self.stream_callback:
                    self.stream_callback("", "clear")
                    self.stream_callback(cached_res, False)
                return cached_res
        else:
            self._log("    [Cache BYPASS] force_pipeline ist aktiv. Cache wird ignoriert.")

        if self.stream_callback:
            self.stream_callback("", "clear")

        # ── Provider-Spezifische Einstellungen ermitteln ────────────────────
        if litellm is None:
            raise RuntimeError(
                "LiteLLM ist nicht installiert. Bitte installiere die Projekt-Abhaengigkeiten "
                "mit `py -3.10 -m pip install -r requirements.txt` oder `py -3.10 -m pip install -e .`."
            )

        provider = model.split("/")[0]
        # Mapping von LiteLLM-Präfixen auf interne Config-Keys
        provider_map = {
            "gemini": "google",
            "vertex_ai": "google"
        }
        config_provider = provider_map.get(provider, provider)
        
        providers_cfg = self.llm_config.get("providers", {})
        prov_cfg = providers_cfg.get(config_provider, {})

        api_base = prov_cfg.get("api_base")
        api_key = prov_cfg.get("api_key")
        
        import os
        # Setze Umgebungsvariablen als Fallback für LiteLLM
        if config_provider == "google" and api_key:
            os.environ["GEMINI_API_KEY"] = api_key
        elif config_provider == "openai" and api_key:
            os.environ["OPENAI_API_KEY"] = api_key
        elif config_provider == "mistral" and api_key:
            os.environ["MISTRAL_API_KEY"] = api_key

        # Vision-Struktur aufbauen, falls Bild vorhanden (OpenAI-kompatibles Format)
        if image_path:
            base64_image = self._encode_image(image_path)
            ext = Path(image_path).suffix.lower()
            mime_type = "image/jpeg"
            if ext == ".png":
                mime_type = "image/png"
            elif ext == ".gif":
                mime_type = "image/gif"
            elif ext == ".webp":
                mime_type = "image/webp"

            messages = [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": user_prompt},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{mime_type};base64,{base64_image}"
                            }
                        }
                    ]
                }
            ]
        else:
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ]

        # LiteLLM Arguments
        completion_kwargs = {
            "model": model,
            "messages": messages,
            "stream": True,
            "max_tokens": max_tokens
        }
        if api_base:
            completion_kwargs["api_base"] = api_base
        if api_key:
            completion_kwargs["api_key"] = api_key

        # think-Parameter für Ollama als Extra-Parameter durchreichen
        if provider == "ollama":
            completion_kwargs["extra_body"] = {"think": bool(think)}

        # GPU-Serialisierung nur bei lokalem Ollama erzwingen
        is_ollama = (provider == "ollama")
        lock_to_use = _OLLAMA_LOCK if is_ollama else _DUMMY_LOCK

        last_exc = None
        with lock_to_use:
            for attempt in range(3):
                try:
                    self._log(f"    → LiteLLM ({model}) [Versuch {attempt + 1}/3]...")
                    
                    # litellm API-Call
                    response = litellm.completion(**completion_kwargs)
                    result = self._process_stream(response, think_enabled=think, max_tokens=max_tokens)
                    
                    # Bei erfolgreicher Abfrage Cache befüllen / aktualisieren
                    if result:
                        self.cache.set(raw_text_content, model, p_ver, result)
                        
                    mode = "🧠 Thinking" if think else "⚡ Standard"
                    self._log(f"    → Fertig [{mode}].")
                    return result

                except Exception as e:
                    last_exc = e
                    self._log(f"    → API-Verbindungsfehler: {e}")
                    if attempt < 2:
                        time.sleep(5)

        raise RuntimeError(f"LiteLLM-Anfrage fehlgeschlagen nach 3 Versuchen: {last_exc}")
