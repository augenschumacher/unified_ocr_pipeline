"""
tasks.py – Pipeline-Tasks (Domain-Logik aller LLM-Aufrufe)

LLMClient erweitert OllamaClient um die vier Pipeline-Methoden:
    run_glm_ocr()       → Phase 2b: Spezialisiertes Dokumenten-OCR
    run_vision_review() → Phase 3:  Bild-gestützter OCR-Review
    run_page_fusion()   → Phase 4:  Multi-Quellen Text-Fusion
    run_analysis()      → Phase 6:  Metadaten-Extraktion als JSON

Jede Methode ist ausschließlich für ihre eigene Domänenlogik zuständig.
HTTP-Kommunikation wird an OllamaClient.query() delegiert. Das Caching
erfolgt über strukturierte CacheInput-Objekte mit SHA-256-basierten v2-Keys.
"""

from .ollama_client import OllamaClient
from core.cache import CacheInput, sha256_file, sha256_text
from core.metadata import build_document_excerpt, empty_metadata, normalize_metadata, parse_metadata_response
from core.privacy import is_external_model, redact_sensitive_text

# Eingabelängenbegrenzung (Zeichen) – verhindert Context-Overflow
_MAX_DOCLING_CHARS = 8_000
_MAX_OCR_CHARS     = 6_000
_MAX_PREV_CHARS    = 4_000
_MAX_ANALYSIS_CHARS = 12_000


def _domain_neutral_prompt(selected: str, default: str, *, task: str) -> str:
    """Remove the former medical-only role while retaining user customisation."""
    prompt = (selected or default).strip()
    replacements = {
        "medizinischer OCR-Korrektor": "praeziser OCR-Korrektor",
        "medizinischen Dokumentenverarbeitung": "allgemeinen Dokumentenverarbeitung",
        "medizinische Dokumentenverarbeitung": "allgemeine Dokumentenverarbeitung",
        "medizinischer Archivar": "professioneller Dokumentenarchivar",
    }
    for old, new in replacements.items():
        prompt = prompt.replace(old, new).replace(old.capitalize(), new.capitalize())

    unsafe_legacy_markers = {
        "vision": ("ergänze fehlendes",),
        "fusion": (
            "fehlerfreien, flüssigen fließtext",
            "standard-ausgabe ist deutsch",
        ),
        "analysis": (
            "sonst heute",
            "sonst das heutige",
            "tags: 3-5",
            "tags:          3",
        ),
    }
    if any(marker in prompt.casefold() for marker in unsafe_legacy_markers.get(task, ())):
        return default

    if task == "analysis":
        if prompt != default:
            return prompt + "\n\n" + default
    return prompt


class LLMClient(OllamaClient):
    """
    Erweiterung von OllamaClient um alle Pipeline-spezifischen Tasks.
    Unterstützt Caching-Weiterleitung und force_pipeline Bypass.
    """

    def __init__(
        self,
        vision_model:   str,
        fusion_model:   str,
        analysis_model: str,
        glm_ocr_model:  str  = None,
        prompts:        dict = None,
        log_callback         = None,
        think_fusion:   bool = False,
        think_analysis: bool = False,
        keep_alive:     str  = "15m",
        prompt_version: int  = 1,
        force_pipeline: bool = False,
        redact_cloud_inputs: bool = False,
    ):
        super().__init__(prompts=prompts, log_callback=log_callback, keep_alive=keep_alive)
        self.vision_model    = vision_model
        self.fusion_model    = fusion_model
        self.analysis_model  = analysis_model
        self.glm_ocr_model   = glm_ocr_model or "glm-ocr:bf16"
        self.think_fusion    = think_fusion
        self.think_analysis  = think_analysis
        self.prompt_version  = prompt_version
        self.force_pipeline  = force_pipeline
        self.redact_cloud_inputs = redact_cloud_inputs

    def _query_with_privacy(self, model: str, system_prompt: str, user_prompt: str, *args, raw_text: str = "", **kwargs):
        if self.redact_cloud_inputs and is_external_model(model):
            user_prompt = redact_sensitive_text(user_prompt)
            raw_text = redact_sensitive_text(raw_text)
            self._log(f"  Datenschutz: Texteingabe fuer externes Modell redigiert ({model}).")
        return self.query(model, system_prompt, user_prompt, *args, raw_text=raw_text, **kwargs)

    # ------------------------------------------------------------------ #
    #  Phase 2b – GLM-OCR                                                  #
    # ------------------------------------------------------------------ #

    def run_glm_ocr(self, image_path: str, page_num: int) -> str:
        """
        Spezialisiertes Dokumenten-OCR via GLM-OCR Modell.
        Nutzt SHA-256 des Bildes als image_sha256-Bestandteil des v2-Cache-Keys.
        """
        if not self.glm_ocr_model or self.glm_ocr_model in ("Keins", "", "Kein GLM-OCR"):
            return ""

        img_hash = sha256_file(image_path) or f"image_{page_num}"

        sys_prompt = (
            "Du bist ein präzises OCR-System für Dokumentenanalyse. "
            "Extrahiere ALLE Texte exakt wie sie im Dokument erscheinen. "
            "Strukturerhalt: Tabellen als Markdown, Absätze als Absätze, Listen als Listen. "
            "Besonders wichtig: Zahlen, Beträge, Datumsangaben, Codes und Sonderzeichen. "
            "Gib NUR den extrahierten Text zurück. Keine Kommentare."
        )
        self._log(f"  GLM-OCR Seite {page_num}...")
        user_prompt = f"Extrahiere den vollstaendigen Text aus Seite {page_num}."
        cache_input = CacheInput(
            task="glm_ocr",
            system_prompt_hash=sha256_text(sys_prompt),
            user_prompt_hash=sha256_text(user_prompt),
            image_sha256=img_hash,
            options={"page_num": page_num},
        )
        try:
            return self._query_with_privacy(
                self.glm_ocr_model, sys_prompt,
                user_prompt,
                image_path, think=False, max_tokens=4096,
                raw_text=img_hash,
                cache_input=cache_input,
            )
        except Exception as e:
            self._log(f"  GLM-OCR Fehler Seite {page_num}: {e}")
            return ""   # Stiller Fallback

    # ------------------------------------------------------------------ #
    #  Phase 3 – Vision-Review & Bildbeschreibung                          #
    # ------------------------------------------------------------------ #

    def run_image_description(self, image_path: str, page_num: int) -> str:
        """
        Generiert eine detaillierte Beschreibung des Bildes/der Seite.
        Nutzt SHA-256 des Bildes als image_sha256-Bestandteil des v2-Cache-Keys.
        """
        if not self.vision_model or self.vision_model == "Keins":
            return ""

        img_hash = sha256_file(image_path) or f"image_desc_{page_num}"

        default_sys = (
            "Du bist ein präzises Vision-Modell zur Bildbeschreibung. "
            "Beschreibe das übergebene Bild detailliert auf Deutsch. "
            "Erfasse visuelle Elemente, Diagramme, Grafiken, Zeichnungen, "
            "Fotos oder handschriftliche Skizzen sowie eventuell vorhandenen kurzen Text. "
            "Gib NUR die Beschreibung zurück. Keine Einleitung, kein 'Hier ist die Beschreibung'."
        )
        user_prompt = (
            f"Beschreibe den visuellen Inhalt dieser Seite (Seite {page_num}) detailliert."
        )
        system_prompt = self._get_prompt("image_description", default_sys)
        cache_input = CacheInput(
            task="image_description",
            system_prompt_hash=sha256_text(system_prompt),
            user_prompt_hash=sha256_text(user_prompt),
            image_sha256=img_hash,
            options={"page_num": page_num},
        )
        try:
            return self._query_with_privacy(
                self.vision_model, system_prompt,
                user_prompt, image_path, max_tokens=4096,
                raw_text=img_hash,
                cache_input=cache_input,
            )
        except Exception as e:
            raise RuntimeError(f"Bildbeschreibung-Fehler Seite {page_num}: {e}")

    def run_vision_review(self, image_path: str, page_markdown: str, page_num: int) -> str:
        """
        Prüft und korrigiert das Docling-Markdown anhand des Seitenbildes.
        Cache-Key (v2/SHA-256) berücksichtigt Bildinhalt (image_sha256)
        und page_markdown-Hash (source_hashes), sodass gleicher Text mit
        unterschiedlichem Bild – oder umgekehrt – stets einen neuen Key ergibt.
        """
        if not self.vision_model or self.vision_model == "Keins":
            return ""

        default_sys = (
            "Du bist ein praeziser OCR-Korrektor und Layout-Analyst fuer beliebige Dokumentarten. "
            "Dir wird ein Bild einer Dokumentenseite und das vorläufige Markdown übergeben. "
            "Prüfe Tabellenstrukturen, Lesereihenfolge, Absätze und Formatierungen kritisch anhand des Bildes. "
            "Korrigiere nur bildlich belegte Fehler und ergänze nur tatsächlich Sichtbares. "
            "Erfinde oder glätte keine Namen, Zahlen, Daten, Beträge, Codes oder Aussagen. "
            "WICHTIG: Erkannte Tabellen ZWINGEND mit <table_block>...</table_block> umschließen. "
            "Gib NUR das korrigierte Markdown zurück. Keine Einleitung, keine Kommentare."
        )
        user_prompt = (
            f"Vorläufiges Markdown für Seite {page_num}:\n\n"
            f"```markdown\n{page_markdown}\n```\n\n"
            "Prüfe und korrigiere anhand des beigefügten Bildes."
        )
        system_prompt = _domain_neutral_prompt(
            self._get_prompt("vision", default_sys), default_sys, task="vision"
        )
        cache_input = CacheInput(
            task="vision_review",
            system_prompt_hash=sha256_text(system_prompt),
            user_prompt_hash=sha256_text(user_prompt),
            image_sha256=sha256_file(image_path),
            source_hashes={"page_markdown": sha256_text(page_markdown)},
            options={"page_num": page_num},
        )
        try:
            return self._query_with_privacy(
                self.vision_model, system_prompt,
                user_prompt, image_path, max_tokens=4096,
                raw_text=page_markdown,
                cache_input=cache_input,
            )
        except Exception as e:
            raise RuntimeError(f"Vision-Fehler Seite {page_num}: {e}")

    # ------------------------------------------------------------------ #
    #  Phase 4 – Text-Fusion                                               #
    # ------------------------------------------------------------------ #

    def run_page_fusion(
        self,
        ocr_text:              str,
        intermediate_markdown: str,
        page_num:              int,
        previous_page_text:    str  = "",
        is_tabular:            bool = False,
        glm_ocr_text:          str  = "",
    ) -> str:
        """
        Kombiniert alle Quellen zu einem sauberen, fehlerfreien Text.
        Der v2-Cache-Key (SHA-256) wird aus den SHA-256-Hashes aller
        Quelltexte (ocr_text, vision_markdown, glm_ocr_text,
        previous_page_text) und den Optionen gebildet. Gleicher OCR-Text
        mit unterschiedlichem Vision-Markdown erzeugt stets einen anderen Key.
        """
        if not self.fusion_model or self.fusion_model == "Keins":
            return ""

        inter_c = intermediate_markdown[:_MAX_DOCLING_CHARS]
        ocr_c   = ocr_text[:_MAX_OCR_CHARS]
        glm_c   = glm_ocr_text[:_MAX_OCR_CHARS] if glm_ocr_text else ""
        prev_c  = previous_page_text[:_MAX_PREV_CHARS] if previous_page_text else ""

        if len(intermediate_markdown) > _MAX_DOCLING_CHARS:
            self._log(f"    → Vision-Markdown auf {_MAX_DOCLING_CHARS} Zeichen gekürzt.")
        if len(ocr_text) > _MAX_OCR_CHARS:
            self._log(f"    → OCR-Text auf {_MAX_OCR_CHARS} Zeichen gekürzt.")

        parts = []
        if prev_c:
            parts.append(f"--- KONTEXT VORHERIGE SEITE (nur für Seitenübergänge!) ---\n{prev_c}")
        parts.append(f"--- Vision-Review Markdown (Hauptquelle) ---\n{inter_c}")
        if glm_c:
            parts.append(f"--- GLM-OCR Text (strukturierte Rohextraktion) ---\n{glm_c}")
        parts.append(f"--- OCR Rohtext / Sidecar (Absicherung) ---\n{ocr_c}")
        combined = "\n\n".join(parts)

        if is_tabular:
            default_sys = (
                "Du bist ein KI-Assistent für layouttreue Tabellenverarbeitung. "
                "Dir liegen bis zu drei Quellen vor: Vision-Markdown, GLM-OCR, OCR-Rohtext. "
                "Tabellenzeilen, Beträge und Codes VOLLSTÄNDIG rekonstruieren – nichts weglassen. "
                "Deutsche Umlaute (ä, ö, ü, ß) korrekt. Unsichere Werte als [UNSICHER: Wert]. "
                "<table_block>...</table_block> ABSOLUT UNVERÄNDERT übernehmen. "
                "Nur den fertigen Text zurückgeben. Keine Kommentare."
            )
        else:
            default_sys = (
                "Du bist ein KI-Assistent für wortgetreue, allgemeine Dokumentenverarbeitung. "
                "Dir liegen bis zu drei Quellen vor: Vision-Markdown (Hauptquelle), "
                "GLM-OCR (Rohextraktion), OCR-Sidecar (Absicherung). "
                "Rekonstruiere den Inhalt dieser einen Seite quellennah; glätte, deute oder ergänze nichts. "
                "Bewahre die erkannte Dokumentensprache sowie Namen, Zahlen, Daten, Beträge und Codes exakt. "
                "Seitenkontext NUR für Übergänge nutzen – vorherige Seite nicht wiederholen! "
                "Überschriften, Listen und Tabellen beibehalten. "
                "<table_block>...</table_block> ABSOLUT UNVERÄNDERT übernehmen. "
                "Nur den finalen Text zurückgeben. Keine Kommentare."
            )

        system_prompt = _domain_neutral_prompt(
            self._get_prompt("fusion", default_sys), default_sys, task="fusion"
        )
        user_prompt = f"Rohdaten fuer Seite {page_num}:\n\n{combined}\n\nErstelle den finalen Text."
        cache_input = CacheInput(
            task="page_fusion",
            system_prompt_hash=sha256_text(system_prompt),
            user_prompt_hash=sha256_text(user_prompt),
            source_hashes={
                "ocr_text": sha256_text(ocr_text),
                "vision_markdown": sha256_text(intermediate_markdown),
                "glm_ocr_text": sha256_text(glm_ocr_text),
                "previous_page_text": sha256_text(previous_page_text),
            },
            options={
                "page_num": page_num,
                "is_tabular": bool(is_tabular),
                "think": bool(self.think_fusion),
                "limits": {
                    "docling": _MAX_DOCLING_CHARS,
                    "ocr": _MAX_OCR_CHARS,
                    "previous_page": _MAX_PREV_CHARS,
                },
            },
        )

        return self._query_with_privacy(
            self.fusion_model,
            system_prompt,
            user_prompt,
            think=self.think_fusion,
            max_tokens=4096,
            raw_text=ocr_text,
            cache_input=cache_input,
        )

    # ------------------------------------------------------------------ #
    #  Phase 6 – Metadaten-Analyse                                         #
    # ------------------------------------------------------------------ #

    def run_analysis(self, fused_text: str) -> dict:
        """
        Extrahiert und validiert archivische Metadaten als Dictionary.

        Fehlende Werte bleiben unbekannt. Der Dokumentauszug enthält deterministisch
        Anfang, Mitte und Ende, damit Absender, Anlagen und Schlussbereiche nicht
        systematisch unberücksichtigt bleiben.
        """
        if not self.analysis_model or self.analysis_model == "Keins":
            return {}

        default_sys = (
            "Du bist ein professioneller, domain-neutraler Dokumentenarchivar. "
            "Extrahiere ausschließlich Angaben, die im Dokument belegt sind. "
            "Erfinde nichts und leite keine Person nur aus einem Ordnernamen ab. "
            "Wenn ein Wert fehlt oder widersprüchlich ist, verwende null beziehungsweise eine leere Liste. "
            "Insbesondere darf ein fehlendes Dokumentdatum NIEMALS durch das heutige Datum ersetzt werden.\n\n"
            "Antworte ausschließlich mit genau einem JSON-Objekt nach diesem Vertrag:\n"
            "{\n"
            '  "document_date": "YYYY-MM-DD oder null",\n'
            '  "title": "kurzer menschenlesbarer Titel oder null",\n'
            '  "document_type": "Dokumentart oder null",\n'
            '  "tags": ["kontrolliertes Stichwort", "..."],\n'
            '  "issuer": "Aussteller/Absender oder null",\n'
            '  "recipient": "Empfänger/Adressat oder null",\n'
            '  "owner": "eindeutig belegter Akteninhaber/Eigentümer oder null",\n'
            '  "language": "ISO-639-Sprachcode oder null",\n'
            '  "reference_ids": [{"type": "z.B. vertragsnummer", "value": "exakter Wert"}],\n'
            '  "period": {"start": "YYYY-MM-DD oder null", "end": "YYYY-MM-DD oder null", "label": null},\n'
            '  "amount": "Dezimalbetrag ohne Tausendertrennzeichen oder null",\n'
            '  "currency": "ISO-4217-Code oder null",\n'
            '  "field_confidence": {"document_date": 0.0, "title": 0.0},\n'
            '  "evidence": {"document_date": [{"quote": "kurzer Originalbeleg", "page": 1}]}\n'
            "}\n"
            "Konfidenzen gelten pro Feld und liegen zwischen 0 und 1. "
            "Belegzitate müssen wortgetreu aus der Eingabe stammen; Seitenzahlen nur nennen, wenn erkennbar."
        )
        system_prompt = _domain_neutral_prompt(
            self._get_prompt("analysis", default_sys), default_sys, task="analysis"
        )
        text_input = build_document_excerpt(fused_text, max_chars=_MAX_ANALYSIS_CHARS)
        user_prompt = (
            "Analysiere die folgende Dokumentrepräsentation. Bereiche sind Auszüge "
            "aus Anfang, Mitte und Ende desselben Dokuments.\n\n" + text_input
        )
        cache_input = CacheInput(
            task="metadata_analysis",
            system_prompt_hash=sha256_text(system_prompt),
            user_prompt_hash=sha256_text(user_prompt),
            source_hashes={"fused_text": sha256_text(fused_text)},
            options={
                "think": bool(self.think_analysis),
                "schema": "unified_ocr_archival_metadata_v2",
                "excerpt_strategy": "head_middle_tail",
                "max_chars": _MAX_ANALYSIS_CHARS,
            },
        )

        for attempt in range(2):
            try:
                res = self._query_with_privacy(
                    self.analysis_model,
                    system_prompt,
                    user_prompt,
                    think=self.think_analysis,
                    max_tokens=2048,
                    raw_text=fused_text,
                    cache_input=CacheInput(
                        task=cache_input.task,
                        system_prompt_hash=cache_input.system_prompt_hash,
                        user_prompt_hash=cache_input.user_prompt_hash,
                        source_hashes=cache_input.source_hashes,
                        options={**cache_input.options, "attempt": attempt},
                    ),
                )
                parsed = parse_metadata_response(res)
                if parsed is not None:
                    return normalize_metadata(parsed, source_text=fused_text)
                self._log(f"  Metadatenanalyse: unlesbares JSON (Versuch {attempt + 1}).")
            except Exception as exc:
                self._log(f"  Metadatenanalyse fehlgeschlagen (Versuch {attempt + 1}): {exc}")
        return empty_metadata()

    # ------------------------------------------------------------------ #
    #  Phase 8 – Dokumenten-Klassifikation                               #
    # ------------------------------------------------------------------ #

    def run_classification(
        self,
        fused_text: str,
        metadata: dict,
        known_paths: list,
        valid_persons: list,
        path_contexts: dict | None = None,
        memory_candidates: list[dict] | None = None,
    ) -> dict:
        """
        Klassifiziert das Dokument.
        """
        from core.cloud.classifier import classify_document
        return classify_document(fused_text, metadata, known_paths, self, valid_persons, path_contexts, memory_candidates)
