"""
tasks.py – Pipeline-Tasks (Domain-Logik aller LLM-Aufrufe)

LLMClient erweitert OllamaClient um die vier Pipeline-Methoden:
    run_glm_ocr()       → Phase 2b: Spezialisiertes Dokumenten-OCR
    run_vision_review() → Phase 3:  Bild-gestützter OCR-Review
    run_page_fusion()   → Phase 4:  Multi-Quellen Text-Fusion
    run_analysis()      → Phase 6:  Metadaten-Extraktion als JSON

Jede Methode ist ausschließlich für ihre eigene Domänenlogik zuständig.
HTTP-Kommunikation wird an OllamaClient.query() delegiert unter Übergabe
von `raw_text` zur caching-spezifischen MD5-Signierung.
"""

import json
from .ollama_client import OllamaClient

# Eingabelängenbegrenzung (Zeichen) – verhindert Context-Overflow
_MAX_DOCLING_CHARS = 8_000
_MAX_OCR_CHARS     = 6_000
_MAX_PREV_CHARS    = 4_000


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

    # ------------------------------------------------------------------ #
    #  Phase 2b – GLM-OCR                                                  #
    # ------------------------------------------------------------------ #

    def run_glm_ocr(self, image_path: str, page_num: int) -> str:
        """
        Spezialisiertes Dokumenten-OCR via GLM-OCR Modell.
        Nutzt MD5 des Bildes als raw_text-Cache-Schlüssel.
        """
        if not self.glm_ocr_model or self.glm_ocr_model in ("Keins", "", "Kein GLM-OCR"):
            return ""

        # MD5-Hash der Bilddatei als Caching-Identifikator erzeugen
        try:
            import hashlib
            h = hashlib.md5()
            with open(image_path, "rb") as f:
                for chunk in iter(lambda: f.read(4096), b""):
                    h.update(chunk)
            img_hash = h.hexdigest()
        except Exception:
            img_hash = f"image_{page_num}"

        sys_prompt = (
            "Du bist ein präzises OCR-System für Dokumentenanalyse. "
            "Extrahiere ALLE Texte exakt wie sie im Dokument erscheinen. "
            "Strukturerhalt: Tabellen als Markdown, Absätze als Absätze, Listen als Listen. "
            "Besonders wichtig: Zahlen, Beträge, Datumsangaben, Codes und Sonderzeichen. "
            "Gib NUR den extrahierten Text zurück. Keine Kommentare."
        )
        self._log(f"  GLM-OCR Seite {page_num}...")
        try:
            return self.query(
                self.glm_ocr_model, sys_prompt,
                f"Extrahiere den vollständigen Text aus Seite {page_num}.",
                image_path, think=False, max_tokens=4096,
                raw_text=img_hash
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
        Nutzt MD5 des Bildes als raw_text-Cache-Schlüssel.
        """
        if not self.vision_model or self.vision_model == "Keins":
            return ""

        # MD5-Hash der Bilddatei als Caching-Identifikator erzeugen
        try:
            import hashlib
            h = hashlib.md5()
            with open(image_path, "rb") as f:
                for chunk in iter(lambda: f.read(4096), b""):
                    h.update(chunk)
            img_hash = h.hexdigest()
        except Exception:
            img_hash = f"image_desc_{page_num}"

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
        try:
            return self.query(
                self.vision_model, self._get_prompt("image_description", default_sys),
                user_prompt, image_path, max_tokens=4096,
                raw_text=img_hash
            )
        except Exception as e:
            raise RuntimeError(f"Bildbeschreibung-Fehler Seite {page_num}: {e}")

    def run_vision_review(self, image_path: str, page_markdown: str, page_num: int) -> str:
        """
        Prüft und korrigiert das Docling-Markdown anhand des Seitenbildes.
        Übergibt page_markdown als raw_text für die Caching-MD5-Generierung.
        """
        if not self.vision_model or self.vision_model == "Keins":
            return ""

        default_sys = (
            "Du bist ein medizinischer OCR-Korrektor und Layout-Analyst. "
            "Dir wird ein Bild einer Dokumentenseite und das vorläufige Markdown übergeben. "
            "Prüfe Tabellenstrukturen, Absätze und Formatierungen kritisch anhand des Bildes. "
            "Korrigiere Fehler, ergänze Fehlendes. "
            "WICHTIG: Erkannte Tabellen ZWINGEND mit <table_block>...</table_block> umschließen. "
            "Gib NUR das korrigierte Markdown zurück. Keine Einleitung, keine Kommentare."
        )
        user_prompt = (
            f"Vorläufiges Markdown für Seite {page_num}:\n\n"
            f"```markdown\n{page_markdown}\n```\n\n"
            "Prüfe und korrigiere anhand des beigefügten Bildes."
        )
        try:
            return self.query(
                self.vision_model, self._get_prompt("vision", default_sys),
                user_prompt, image_path, max_tokens=4096,
                raw_text=page_markdown
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
        Übergibt den ocr_text als raw_text-Caching-Schlüssel.
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
                "Du bist ein KI-Assistent für medizinische Dokumentenverarbeitung. "
                "Dir liegen bis zu drei Quellen vor: Vision-Markdown (Hauptquelle), "
                "GLM-OCR (Rohextraktion), OCR-Sidecar (Absicherung). "
                "Erstelle einen fehlerfreien, flüssigen Fließtext für diese eine Seite. "
                "Deutsch mit korrekten Umlauten (ä, ö, ü, ß). "
                "Seitenkontext NUR für Übergänge nutzen – vorherige Seite nicht wiederholen! "
                "Überschriften, Listen und Tabellen beibehalten. "
                "<table_block>...</table_block> ABSOLUT UNVERÄNDERT übernehmen. "
                "Nur den finalen Text zurückgeben. Keine Kommentare."
            )

        return self.query(
            self.fusion_model,
            self._get_prompt("fusion", default_sys),
            f"Rohdaten für Seite {page_num}:\n\n{combined}\n\nErstelle den finalen Text.",
            think=self.think_fusion,
            max_tokens=4096,
            raw_text=ocr_text
        )

    # ------------------------------------------------------------------ #
    #  Phase 6 – Metadaten-Analyse                                         #
    # ------------------------------------------------------------------ #

    def run_analysis(self, fused_text: str) -> dict:
        """
        Extrahiert Datum, Titel, Dokumententyp und Tags als JSON-Dictionary.
        Übergibt fused_text als raw_text-Caching-Schlüssel.
        """
        if not self.analysis_model or self.analysis_model == "Keins":
            return {}

        default_sys = (
            "Du bist ein medizinischer Archivar. Extrahiere aus dem Text:\n"
            "1. date:          Datum im Format DD-MM-YYYY (aus Dokument, sonst heute)\n"
            "2. title:         Kurztitel ohne Leerzeichen (Unterstriche statt Leerzeichen)\n"
            "3. document_type: Dokumententyp (z.B. Arztbrief, Rechnung, Befund)\n"
            "4. tags:          3–5 relevante Stichworte, kommagetrennt\n"
            'Antwort AUSSCHLIESSLICH als JSON: {"date":"...","title":"...","document_type":"...","tags":"..."}'
        )
        text_input = fused_text[:_MAX_OCR_CHARS]

        for _ in range(2):
            try:
                res = self.query(
                    self.analysis_model,
                    self._get_prompt("analysis", default_sys),
                    text_input,
                    think=self.think_analysis,
                    max_tokens=1024,
                    raw_text=fused_text
                )
                start = res.find("{")
                end   = res.rfind("}") + 1
                if start != -1 and end > start:
                    return json.loads(res[start:end])
            except Exception:
                pass
        return {}

    # ------------------------------------------------------------------ #
    #  Phase 8 – Dokumenten-Klassifikation                               #
    # ------------------------------------------------------------------ #

    def run_classification(self, fused_text: str, metadata: dict, known_paths: list, valid_persons: list) -> dict:
        """
        Klassifiziert das Dokument.
        """
        from core.cloud.classifier import classify_document
        return classify_document(fused_text, metadata, known_paths, self, valid_persons)
