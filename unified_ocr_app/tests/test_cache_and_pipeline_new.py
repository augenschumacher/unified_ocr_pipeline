"""
tests/test_cache_and_pipeline_new.py – Neue Tests für Cache-Keys, Fusion-Fallback,
Multi-Page-QC und Exportpfade.

Testet:
    a) Vision-Cache: gleicher page_markdown, unterschiedliche Bildinhalte → unterschiedliche v2-Keys
    b) Fusion-Cache: gleicher OCR-Text, unterschiedlicher Vision-Markdown → unterschiedliche v2-Keys
    c) _stage_fusion: fusion_model "Keins" → Fallback auf Vision/GLM/Docling/OCR, kein leerer Text
    d) _stage_fusion: run_page_fusion gibt "" zurück → degraded fallback verwendet
    e) Multi-Page-QC: Nachkorrektur reduziert fused_pages NICHT auf {1: full_document_text}
    f) Exportpfade: _resolve_exported_path findet verschobene Dateien über moved_files
"""

import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

from core.cache import CacheInput, build_cache_key, sha256_text


# ---------------------------------------------------------------------------
# a) Vision-Cache: gleicher page_markdown, verschiedene Bilder → anderer Key
# ---------------------------------------------------------------------------
class TestVisionCacheKeyVariants(unittest.TestCase):

    def _make_vision_input(self, image_sha256: str, page_markdown: str) -> CacheInput:
        system_prompt = "Du bist ein OCR-Korrektor."
        user_prompt = f"Vorlaeufigesmarkdown:\n```\n{page_markdown}\n```"
        return CacheInput(
            task="vision_review",
            system_prompt_hash=sha256_text(system_prompt),
            user_prompt_hash=sha256_text(user_prompt),
            image_sha256=image_sha256,
            source_hashes={"page_markdown": sha256_text(page_markdown)},
            options={"page_num": 1},
        )

    def test_same_markdown_different_image_gives_different_key(self):
        """Gleicher page_markdown, unterschiedliche Bildinhalte → unterschiedliche v2-Cache-Keys."""
        markdown = "## Abschnitt\n\nEin Satz mit Zahlen: 1.234,56 EUR."

        key_img_a = build_cache_key(self._make_vision_input("sha256_of_image_a", markdown))
        key_img_b = build_cache_key(self._make_vision_input("sha256_of_image_b", markdown))

        self.assertNotEqual(key_img_a, key_img_b,
            "Verschiedene Bilder mit gleichem Markdown müssen verschiedene v2-Cache-Keys erzeugen.")

    def test_same_image_same_markdown_gives_same_key(self):
        """Gleicher Bildinhalt und Markdown → identischer v2-Cache-Key."""
        markdown = "## Abschnitt\n\nEin Satz mit Zahlen: 1.234,56 EUR."
        img_hash = "abc123def456"

        key1 = build_cache_key(self._make_vision_input(img_hash, markdown))
        key2 = build_cache_key(self._make_vision_input(img_hash, markdown))

        self.assertEqual(key1, key2,
            "Identische Eingaben müssen stets denselben v2-Cache-Key erzeugen.")

    def test_different_markdown_same_image_gives_different_key(self):
        """Gleicher Bildinhalt, unterschiedlicher Markdown → unterschiedliche v2-Cache-Keys."""
        img_hash = "abc123def456"

        key_a = build_cache_key(self._make_vision_input(img_hash, "Seite 1 Text A"))
        key_b = build_cache_key(self._make_vision_input(img_hash, "Seite 1 Text B"))

        self.assertNotEqual(key_a, key_b)

    def test_key_has_v2_prefix(self):
        """Alle v2-Keys müssen mit 'v2:' beginnen."""
        ci = self._make_vision_input("some_hash", "some markdown")
        key = build_cache_key(ci)
        self.assertTrue(key.startswith("v2:"), f"Key hat falsches Präfix: {key[:10]}")


# ---------------------------------------------------------------------------
# b) Fusion-Cache: gleicher OCR, unterschiedlicher Vision-Markdown → anderer Key
# ---------------------------------------------------------------------------
class TestFusionCacheKeyVariants(unittest.TestCase):

    def _make_fusion_input(self, ocr_text: str, vision_markdown: str, page_num: int = 1) -> CacheInput:
        system_prompt = "Du bist ein Fusions-LLM."
        user_prompt = f"Rohdaten fuer Seite {page_num}: ..."
        return CacheInput(
            task="page_fusion",
            system_prompt_hash=sha256_text(system_prompt),
            user_prompt_hash=sha256_text(user_prompt),
            source_hashes={
                "ocr_text": sha256_text(ocr_text),
                "vision_markdown": sha256_text(vision_markdown),
                "glm_ocr_text": sha256_text(""),
                "previous_page_text": sha256_text(""),
            },
            options={
                "page_num": page_num,
                "is_tabular": False,
                "think": False,
            },
        )

    def test_same_ocr_different_vision_gives_different_key(self):
        """Gleicher OCR-Text, unterschiedlicher Vision-Markdown → unterschiedliche v2-Keys."""
        ocr = "Rechnungsbetrag 1.234,56 EUR vom 20.05.2026."

        key_a = build_cache_key(self._make_fusion_input(ocr, "## Vision A\n\nText A."))
        key_b = build_cache_key(self._make_fusion_input(ocr, "## Vision B\n\nText B (komplett anders)."))

        self.assertNotEqual(key_a, key_b,
            "Unterschiedlicher Vision-Markdown muss verschiedene Fusion-Cache-Keys erzeugen.")

    def test_same_ocr_same_vision_gives_same_key(self):
        """Gleiche Eingaben → identischer v2-Key."""
        ocr = "Betrag 500,00 EUR"
        vision = "## Rechnung\n\nBetrag 500,00 EUR"

        key1 = build_cache_key(self._make_fusion_input(ocr, vision))
        key2 = build_cache_key(self._make_fusion_input(ocr, vision))

        self.assertEqual(key1, key2)

    def test_different_ocr_same_vision_gives_different_key(self):
        """Unterschiedlicher OCR-Text, gleicher Vision → unterschiedliche Keys."""
        vision = "## Seite 1"

        key_a = build_cache_key(self._make_fusion_input("OCR text Alpha", vision))
        key_b = build_cache_key(self._make_fusion_input("OCR text Beta", vision))

        self.assertNotEqual(key_a, key_b)


# ---------------------------------------------------------------------------
# c) _stage_fusion: fusion_model "Keins" → Fallback auf beste Quelle
# ---------------------------------------------------------------------------
class TestStageFusionFallbackOnKeins(unittest.TestCase):

    def _make_orchestrator(self, fusion_model: str = "Keins"):
        from core.config import AppConfig
        from core.pipeline import PipelineOrchestrator

        with tempfile.TemporaryDirectory() as tmpdir:
            config = AppConfig(Path(tmpdir))
            mock_llm = MagicMock()
            mock_llm.fusion_model = fusion_model
            orch = PipelineOrchestrator(config=config, llm_client=mock_llm)
            return orch, mock_llm

    def test_fusion_keins_uses_vision_text_as_fallback(self):
        """fusion_model='Keins': Vision-Text wird als Fallback verwendet, kein leerer Text."""
        from core.config import AppConfig
        from core.pipeline import PipelineOrchestrator

        with tempfile.TemporaryDirectory() as tmpdir:
            config = AppConfig(Path(tmpdir))
            mock_llm = MagicMock()
            mock_llm.fusion_model = "Keins"
            mock_llm.run_page_fusion.return_value = ""  # würde auch "" liefern bei Keins

            orch = PipelineOrchestrator(config=config, llm_client=mock_llm)

            image_paths = [Path("dummy_page1.png")]
            ocr_texts = {1: "OCR Rohtext Seite 1."}
            vision_mds = {1: "Vision Markdown Seite 1."}
            page_mds = {1: "Docling Markdown Seite 1."}
            glm_texts = {1: "GLM OCR Seite 1."}

            result = orch._stage_fusion(
                image_paths=image_paths,
                ocr_texts=ocr_texts,
                vision_markdowns=vision_mds,
                page_markdowns=page_mds,
                glm_texts=glm_texts,
                is_tabular=False,
                total_pages=1,
            )

            # _stage_fusion ruft run_page_fusion auf, welches "" zurückgibt,
            # dann wird degraded fallback (Vision) verwendet
            self.assertIn(1, result)
            self.assertNotEqual(result[1], "",
                "Bei fusion_model='Keins' (leere Rückgabe) muss ein Fallback-Text verwendet werden.")
            # Bester verfügbarer Text ist Vision
            self.assertEqual(result[1], "Vision Markdown Seite 1.")

    def test_fusion_keins_falls_back_to_glm_if_no_vision(self):
        """fusion_model='Keins', kein Vision-Text → GLM als Fallback."""
        from core.config import AppConfig
        from core.pipeline import PipelineOrchestrator

        with tempfile.TemporaryDirectory() as tmpdir:
            config = AppConfig(Path(tmpdir))
            mock_llm = MagicMock()
            mock_llm.fusion_model = "Keins"
            mock_llm.run_page_fusion.return_value = ""

            orch = PipelineOrchestrator(config=config, llm_client=mock_llm)

            result = orch._stage_fusion(
                image_paths=[Path("dummy.png")],
                ocr_texts={1: "OCR text."},
                vision_markdowns={1: ""},   # kein Vision-Text
                page_markdowns={1: ""},     # kein Docling
                glm_texts={1: "GLM text."}, # GLM vorhanden
                is_tabular=False,
                total_pages=1,
            )

            self.assertEqual(result[1], "GLM text.")

    def test_fusion_keins_falls_back_to_ocr_if_nothing_else(self):
        """fusion_model='Keins', weder Vision noch GLM → OCR als letzter Fallback."""
        from core.config import AppConfig
        from core.pipeline import PipelineOrchestrator

        with tempfile.TemporaryDirectory() as tmpdir:
            config = AppConfig(Path(tmpdir))
            mock_llm = MagicMock()
            mock_llm.fusion_model = "Keins"
            mock_llm.run_page_fusion.return_value = ""

            orch = PipelineOrchestrator(config=config, llm_client=mock_llm)

            result = orch._stage_fusion(
                image_paths=[Path("dummy.png")],
                ocr_texts={1: "OCR only text."},
                vision_markdowns={1: ""},
                page_markdowns={1: ""},
                glm_texts={1: ""},
                is_tabular=False,
                total_pages=1,
            )

            self.assertEqual(result[1], "OCR only text.")


# ---------------------------------------------------------------------------
# d) _stage_fusion: run_page_fusion gibt "" zurück → degraded fallback
# ---------------------------------------------------------------------------
class TestStageFusionDegradedFallback(unittest.TestCase):

    def test_empty_fusion_result_triggers_degraded_fallback(self):
        """Wenn run_page_fusion '' zurückgibt, wird der degraded fallback verwendet."""
        from core.config import AppConfig
        from core.pipeline import PipelineOrchestrator

        with tempfile.TemporaryDirectory() as tmpdir:
            config = AppConfig(Path(tmpdir))
            mock_llm = MagicMock()
            mock_llm.fusion_model = "some-model"
            # run_page_fusion gibt leeren String zurück (z.B. Netzwerkfehler etc.)
            mock_llm.run_page_fusion.return_value = ""

            orch = PipelineOrchestrator(config=config, llm_client=mock_llm)

            result = orch._stage_fusion(
                image_paths=[Path("dummy.png")],
                ocr_texts={1: "OCR Fallback Text."},
                vision_markdowns={1: "Vision Fallback Text."},
                page_markdowns={1: "Docling Fallback."},
                glm_texts={1: "GLM Fallback."},
                is_tabular=False,
                total_pages=1,
            )

            # Vision hat höchste Priorität im Fallback
            self.assertEqual(result[1], "Vision Fallback Text.",
                "Leere Fusion-Rückgabe muss Vision-Text als degraded fallback verwenden.")

    def test_whitespace_only_fusion_result_triggers_degraded_fallback(self):
        """Auch ein Nur-Leerzeichen-Ergebnis zählt als leer → degraded fallback."""
        from core.config import AppConfig
        from core.pipeline import PipelineOrchestrator

        with tempfile.TemporaryDirectory() as tmpdir:
            config = AppConfig(Path(tmpdir))
            mock_llm = MagicMock()
            mock_llm.fusion_model = "some-model"
            mock_llm.run_page_fusion.return_value = "   \n  "

            orch = PipelineOrchestrator(config=config, llm_client=mock_llm)

            result = orch._stage_fusion(
                image_paths=[Path("dummy.png")],
                ocr_texts={1: "OCR Text."},
                vision_markdowns={1: "Vision Text."},
                page_markdowns={1: ""},
                glm_texts={1: ""},
                is_tabular=False,
                total_pages=1,
            )

            self.assertEqual(result[1], "Vision Text.")


# ---------------------------------------------------------------------------
# e) Multi-Page-QC: Nachkorrektur reduziert fused_pages NICHT auf {1: full_text}
# ---------------------------------------------------------------------------
class TestMultiPageQCPreservesPages(unittest.TestCase):

    @patch("core.pipeline.shutil.move")
    def test_qc_correction_does_not_collapse_fused_pages(self, mock_move):
        """Dokumentweite Nachkorrektur darf fused_pages nicht auf {1: full_document_text} reduzieren."""
        from core.config import AppConfig
        from core.pipeline import PipelineOrchestrator

        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            config = AppConfig(tmpdir_path)
            config.base_dir = tmpdir_path
            config.final_dir.mkdir(parents=True, exist_ok=True)

            mock_llm = MagicMock()
            mock_llm.vision_model = "vision-model"
            mock_llm.glm_ocr_model = "Keins"
            mock_llm.fusion_model = "fusion-model"

            long_text = "X" * 200  # > 150 Zeichen, kein low-text-Pfad

            orch = PipelineOrchestrator(
                config=config,
                llm_client=mock_llm,
                save_docx_enabled=False,
                save_json_enabled=False,
                gdrive_enabled=False,
            )

            # 2 Seiten, Fusion liefert seitenweise Text
            mock_llm.run_vision_review.side_effect = lambda path, md, num: f"vision seite {num}"
            mock_llm.run_page_fusion.side_effect = lambda **kwargs: f"fused seite {kwargs.get('page_num')}"

            orch._stage_prepare = MagicMock(return_value=Path("dummy_work.pdf"))
            orch._stage_ocrmypdf = MagicMock(return_value=(Path("dummy_ocr.pdf"), long_text))
            orch._stage_docling = MagicMock(return_value=(long_text, {1: long_text, 2: long_text}))
            orch._stage_extract_pages = MagicMock(
                return_value=([Path("p1.png"), Path("p2.png")], {1: long_text, 2: long_text})
            )

            # QC ändert den zusammengefügten Text (simuliert eine Nachkorrektur)
            corrected_full_text = "Vollstaendig korrigierter Gesamttext des Dokuments."
            orch._stage_quality = MagicMock(
                side_effect=lambda o, d, v, f: (corrected_full_text, {"warnings": []})
            )
            orch._stage_analysis = MagicMock(return_value=({}, "doc"))
            orch._stage_export = MagicMock()
            orch._stage_organize = MagicMock(return_value=([], ""))

            dummy_input = tmpdir_path / "input.pdf"
            dummy_input.write_text("input", encoding="utf-8")

            orch.process_file(dummy_input)

            orch._stage_export.assert_called_once()
            args = orch._stage_export.call_args[0]
            # args[1] = fused_pages, args[2] = fused_text
            fused_pages = args[1]
            fused_text = args[2]

            # fused_text soll der korrigierte Gesamttext sein
            self.assertEqual(fused_text, corrected_full_text,
                "fused_text muss den QC-korrigierten Gesamttext enthalten.")

            # fused_pages soll NICHT auf {1: corrected_full_text} zusammengefasst sein
            self.assertNotEqual(
                fused_pages,
                {1: corrected_full_text},
                "fused_pages darf nach QC-Nachkorrektur NICHT auf {1: full_document_text} reduziert werden.",
            )

            # fused_pages soll beide Seiten enthalten
            self.assertIn(1, fused_pages, "fused_pages muss Seite 1 enthalten.")
            self.assertIn(2, fused_pages, "fused_pages muss Seite 2 enthalten.")

            # Seiteninhalt stammt aus Fusion (nicht aus korrigiertem Gesamttext)
            self.assertIn("fused seite 1", fused_pages[1],
                "fused_pages[1] soll den Fusion-Text von Seite 1 enthalten.")
            self.assertIn("fused seite 2", fused_pages[2],
                "fused_pages[2] soll den Fusion-Text von Seite 2 enthalten.")


# ---------------------------------------------------------------------------
# f) Exportpfade: _resolve_exported_path findet Dateien über moved_files
# ---------------------------------------------------------------------------
class TestResolveExportedPath(unittest.TestCase):

    def setUp(self):
        from core.config import AppConfig
        from core.pipeline import PipelineOrchestrator
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmpdir_path = Path(self._tmpdir.name)
        config = AppConfig(self.tmpdir_path)
        self.orch = PipelineOrchestrator(config=config, llm_client=MagicMock())

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_resolve_path_from_dict_when_file_exists(self):
        """Wenn die Datei am ursprünglichen Pfad liegt, wird sie direkt zurückgegeben."""
        pdf = self.tmpdir_path / "doc.pdf"
        pdf.write_text("pdf", encoding="utf-8")

        exported_paths = {"pdf": pdf, "txt": None}
        result = self.orch._resolve_exported_path(exported_paths, "pdf")

        self.assertEqual(result, pdf)

    def test_resolve_path_from_moved_files(self):
        """Datei wurde verschoben: _resolve_exported_path findet sie über moved_files."""
        original_dir = self.tmpdir_path / "final"
        original_dir.mkdir()
        moved_dir = self.tmpdir_path / "final" / "Jan" / "Rechnungen"
        moved_dir.mkdir(parents=True)

        # Datei liegt NICHT mehr am ursprünglichen Pfad, sondern im moved-Verzeichnis
        moved_pdf = moved_dir / "doc.pdf"
        moved_pdf.write_text("pdf content", encoding="utf-8")

        original_path = original_dir / "doc.pdf"  # existiert NICHT mehr

        exported_paths = {"pdf": original_path}
        moved_files = [str(moved_pdf)]

        result = self.orch._resolve_exported_path(exported_paths, "pdf", moved_files)

        self.assertIsNotNone(result, "Datei sollte über moved_files gefunden werden.")
        self.assertEqual(result, moved_pdf)

    def test_resolve_path_returns_none_for_nonexistent_and_no_moved(self):
        """Datei existiert weder am ursprünglichen Pfad noch in moved_files → None."""
        nonexistent = self.tmpdir_path / "ghost.pdf"
        exported_paths = {"pdf": nonexistent}

        result = self.orch._resolve_exported_path(exported_paths, "pdf", moved_files=None)

        self.assertIsNone(result)

    def test_resolve_path_returns_none_for_non_dict_exported_paths(self):
        """Wenn exported_paths kein dict ist, wird None zurückgegeben."""
        result = self.orch._resolve_exported_path(None, "pdf")
        self.assertIsNone(result)

        result2 = self.orch._resolve_exported_path("not_a_dict", "pdf")
        self.assertIsNone(result2)

    def test_resolve_finds_correct_file_among_multiple_moved(self):
        """moved_files enthält mehrere Dateien; nur die mit passendem Namen wird zurückgegeben."""
        moved_dir = self.tmpdir_path / "dest"
        moved_dir.mkdir()

        pdf = moved_dir / "my_document.pdf"
        txt = moved_dir / "my_document.txt"
        pdf.write_text("pdf", encoding="utf-8")
        txt.write_text("txt", encoding="utf-8")

        original_pdf = self.tmpdir_path / "my_document.pdf"  # nicht vorhanden
        exported_paths = {"pdf": original_pdf, "txt": original_pdf.with_suffix(".txt")}

        moved_files = [str(txt), str(pdf)]

        pdf_result = self.orch._resolve_exported_path(exported_paths, "pdf", moved_files)
        self.assertEqual(pdf_result, pdf)


if __name__ == "__main__":
    unittest.main()
