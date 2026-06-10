"""
tests/test_pipeline.py – Unit-Tests für die OCR-Pipeline

Getestete Komponenten:
    - SettingsManager  (core.settings)
    - QualityChecker   (core.quality)
    - save_markdown_as_docx (core.docx_tools)
    - LLMClient        (core.llm) – Initialisierung ohne echtes Ollama
    - OllamaClient     (core.llm.ollama_client) – Payload-Builder
"""

import unittest
from unittest.mock import patch, MagicMock
from pathlib import Path
import json
import tempfile

from core.settings  import SettingsManager
from core.quality   import QualityChecker
from core.docx_tools import save_markdown_as_docx
from core.llm       import LLMClient
from core.llm.ollama_client import OllamaClient


class TestSettingsManager(unittest.TestCase):

    def test_defaults(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr      = SettingsManager(Path(tmpdir) / "settings.json")
            settings = mgr.load()
            self.assertEqual(settings["base_dir"], "C:\\OCR_Workdir")
            self.assertEqual(settings["output_format"], "PDF und DOCX")
            self.assertEqual(settings["models"]["vision"], "qwen3-vl:30b-a3b-instruct-q4_K_M")
            self.assertEqual(settings["models"]["glm_ocr"], "glm-ocr:bf16")
            self.assertFalse(settings["think_fusion"])
            self.assertFalse(settings["think_analysis"])
            self.assertTrue(settings["unload_models_enabled"])
            self.assertTrue(settings["system_tray_enabled"])
            self.assertFalse(settings["review_before_save"])
            self.assertEqual(settings["privacy_mode"], "standard")

    def test_validation_bad_format(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr      = SettingsManager(Path(tmpdir) / "settings.json")
            settings = mgr.load()
            settings["output_format"] = "UNSUPPORTED"
            with self.assertRaises(ValueError):
                mgr.save(settings)

    def test_validation_bad_model_type(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr      = SettingsManager(Path(tmpdir) / "settings.json")
            settings = mgr.load()
            settings["models"]["vision"] = 12345
            with self.assertRaises(ValueError):
                mgr.save(settings)

    def test_roundtrip(self):
        """Gespeicherte Settings werden beim nächsten Laden korrekt wiederhergestellt."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "settings.json"
            mgr  = SettingsManager(path)
            s    = mgr.load()
            s["think_fusion"]   = True
            s["think_analysis"] = True
            s["models"]["glm_ocr"] = "glm-ocr:bf16"
            s["unload_models_enabled"] = False
            s["system_tray_enabled"]   = False
            s["review_before_save"]    = True
            mgr.save(s)

            mgr2 = SettingsManager(path)
            s2   = mgr2.load()
            self.assertTrue(s2["think_fusion"])
            self.assertTrue(s2["think_analysis"])
            self.assertEqual(s2["models"]["glm_ocr"], "glm-ocr:bf16")
            self.assertFalse(s2["unload_models_enabled"])
            self.assertFalse(s2["system_tray_enabled"])
            self.assertTrue(s2["review_before_save"])

    def test_privacy_mode_validation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = SettingsManager(Path(tmpdir) / "settings.json")
            settings = mgr.load()
            settings["privacy_mode"] = "local_only"
            mgr.save(settings)
            self.assertEqual(SettingsManager(Path(tmpdir) / "settings.json").load()["privacy_mode"], "local_only")

            settings["privacy_mode"] = "cloud_first"
            with self.assertRaises(ValueError):
                mgr.save(settings)


class TestQualityChecker(unittest.TestCase):

    def test_normalize_amount(self):
        self.assertEqual(QualityChecker.normalize_amount("1.234,56"),    "1234,56")
        self.assertEqual(QualityChecker.normalize_amount("-12,34"),      "-12,34")
        self.assertEqual(QualityChecker.normalize_amount("+1.234.567,89"), "1234567,89")

    def test_extract_amounts_and_dates(self):
        text    = "Betrag: 1.234,56 EUR, Abzug: -150,00 EUR. Fällig am 20.05.2026."
        amounts = QualityChecker.extract_amounts(text)
        dates   = QualityChecker.extract_dates(text)
        self.assertIn("1234,56",    amounts)
        self.assertIn("-150,00",   amounts)
        self.assertIn("20.05.2026", dates)

    def test_run_checks_clean(self):
        ocr   = "Rechnungsbetrag 1.234,56 EUR vom 20.05.2026."
        doc   = "| Wert |\n|---|\n| 1.234,56 |"
        vis   = "Rechnung 1.234,56 EUR."
        fused = "Rechnung 1.234,56 EUR vom 20.05.2026."
        r = QualityChecker.run_quality_check(ocr, doc, vis, fused)
        self.assertEqual(r["severity"], "info")
        self.assertEqual(len(r["warnings"]), 0)

    def test_run_checks_missing_amount(self):
        ocr   = "Betrag 1.234,56 EUR vom 20.05.2026."
        doc   = "Tabelle | 1.234,56 |"
        vis   = "1.234,56"
        fused = "Rechnung vom 20.05.2026."   # Betrag fehlt → auch Ziffernverlust
        r = QualityChecker.run_quality_check(ocr, doc, vis, fused)
        self.assertEqual(r["severity"], "error")
        self.assertTrue(any("Geldbetrag fehlt" in w for w in r["warnings"]))
        self.assertTrue(any(mv["value"] == "1.234,56" for mv in r["missing_values"]))


class TestOllamaClient(unittest.TestCase):

    def test_think_false_sent_to_all_normal_models(self):
        """Alle normalen LLMs bekommen think=False wenn Checkbox aus."""
        client = OllamaClient()
        for model in ("gemma4:26b", "qwen3:27b", "llama3:8b", "mistral:7b", "deepseek-r1:14b"):
            with self.subTest(model=model):
                p = client._build_payload(model, "sys", "usr", think=False)
                self.assertIn("think", p, f"{model}: think-Key muss vorhanden sein")
                self.assertFalse(p["think"], f"{model}: think muss False sein")

    def test_think_true_sent_to_all_normal_models(self):
        """Alle normalen LLMs bekommen think=True wenn Checkbox an."""
        client = OllamaClient()
        for model in ("gemma4:26b", "qwen3:27b", "llama3:8b", "deepseek-r1:14b"):
            with self.subTest(model=model):
                p = client._build_payload(model, "sys", "usr", think=True)
                self.assertIn("think", p)
                self.assertTrue(p["think"])

    def test_glm_ocr_never_gets_think(self):
        """GLM-OCR ist ein reines OCR-Modell – bekommt keinen think-Parameter."""
        client = OllamaClient()
        for think_val in (True, False):
            with self.subTest(think=think_val):
                p = client._build_payload("glm-ocr:bf16", "sys", "usr", think=think_val)
                self.assertNotIn("think", p)

    def test_keep_alive_param(self):
        """OllamaClient.keep_alive wird korrekt in der Payload gesetzt."""
        client = OllamaClient(keep_alive="0")
        p = client._build_payload("qwen3:27b", "sys", "usr")
        self.assertEqual(p["keep_alive"], "0")

        client2 = OllamaClient(keep_alive="15m")
        p2 = client2._build_payload("qwen3:27b", "sys", "usr")
        self.assertEqual(p2["keep_alive"], "15m")

    def test_process_stream_safety_watchdog(self):
        """Der Stream-Watchdog bricht ab, wenn max_tokens überschritten wird (sowohl Content als auch Thinking)."""
        client = OllamaClient()
        
        class MockResponse:
            def __init__(self, lines):
                self.lines = lines
            def iter_lines(self):
                return self.lines

        # Fall 1: Content-Tokens überschreiten das Limit
        lines_content = [
            json.dumps({"message": {"content": f"token{i} "}}).encode("utf-8")
            for i in range(10)
        ]
        res = client._process_stream(MockResponse(lines_content), max_tokens=5)
        # Es sollten nur max 6 Tokens verarbeitet werden (token0 bis token5, da beim 6. Token count=6 > 5 abbricht)
        self.assertTrue(res.startswith("token0"))
        # Split by space and check length
        self.assertLessEqual(len(res.strip().split()), 6)

        # Fall 2: Thinking-Tokens überschreiten das Limit (bei think_enabled=False)
        lines_thinking = [
            json.dumps({"message": {"thinking": f"think{i} "}}).encode("utf-8")
            for i in range(10)
        ]
        # Das darf nicht in eine unendliche Schleife laufen, sondern muss abbrechen
        res_thinking = client._process_stream(MockResponse(lines_thinking), max_tokens=5, think_enabled=False)
        self.assertEqual(res_thinking, "")

    def test_process_stream_repetition_watchdog(self):
        """Der Stream-Watchdog bricht ab, wenn eine Wiederholungsschleife im Stream erkannt wird."""
        client = OllamaClient()
        
        class MockResponse:
            def __init__(self, lines):
                self.lines = lines
                self.consumed_count = 0
            def iter_lines(self):
                for line in self.lines:
                    self.consumed_count += 1
                    yield line

        # Fall 1: Content-Wiederholungsschleife ("104,00 EUR Σ " = 13 Zeichen)
        # 14 Wiederholungen = 182 Zeichen (Schwellenwert 180 überschritten).
        lines_content = [
            json.dumps({"message": {"content": "104,00 EUR \u03a3 "}}).encode("utf-8")
            for _ in range(50)
        ]
        resp_content = MockResponse(lines_content)
        res = client._process_stream(resp_content, max_tokens=1000)
        
        # Sicherstellen, dass die Schleife abgebrochen wurde (consumed_count < 50)
        self.assertLess(resp_content.consumed_count, 50)
        # Und dass wir trotzdem Text extrahiert haben (aber eben nicht die volle Länge)
        self.assertGreater(res.count("104,00 EUR"), 10)

        # Fall 2: Thinking-Wiederholungsschleife bei think_enabled=False ("looping_thought " = 16 Zeichen)
        # 12 Wiederholungen = 192 Zeichen.
        lines_thinking = [
            json.dumps({"message": {"thinking": "looping_thought "}}).encode("utf-8")
            for _ in range(50)
        ]
        resp_thinking = MockResponse(lines_thinking)
        res_thinking = client._process_stream(resp_thinking, max_tokens=1000, think_enabled=False)
        
        # Auch hier muss abgebrochen worden sein
        self.assertLess(resp_thinking.consumed_count, 50)
        self.assertEqual(res_thinking, "")




class TestLLMClient(unittest.TestCase):

    def test_init_defaults(self):
        client = LLMClient(vision_model="v", fusion_model="f", analysis_model="a")
        self.assertEqual(client.glm_ocr_model, "glm-ocr:bf16")
        self.assertFalse(client.think_fusion)
        self.assertFalse(client.think_analysis)
        self.assertIsNone(client.stream_callback)

    def test_get_prompt_fallback(self):
        client = LLMClient(vision_model="v", fusion_model="f", analysis_model="a")
        result = client._get_prompt("vision", "DEFAULT")
        self.assertEqual(result, "DEFAULT")

    def test_get_prompt_custom(self):
        client = LLMClient(
            vision_model="v", fusion_model="f", analysis_model="a",
            prompts={"vision": "Mein Prompt"}
        )
        result = client._get_prompt("vision", "DEFAULT")
        self.assertEqual(result, "Mein Prompt")

    def test_glm_ocr_skip_on_keins(self):
        client = LLMClient(
            vision_model="v", fusion_model="f", analysis_model="a",
            glm_ocr_model="Keins",
        )
        result = client.run_glm_ocr("dummy_path.png", 1)
        self.assertEqual(result, "")

    def test_fusion_skip_on_keins(self):
        client = LLMClient(
            vision_model="v", fusion_model="Keins", analysis_model="a",
        )
        result = client.run_page_fusion("ocr", "markdown", 1)
        self.assertEqual(result, "")

    def test_analysis_skip_on_keins(self):
        client = LLMClient(
            vision_model="v", fusion_model="f", analysis_model="Keins",
        )
        result = client.run_analysis("some text")
        self.assertEqual(result, {})


class TestDocxExport(unittest.TestCase):

    def test_readable_docx(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir) / "test.docx"
            md  = "# Titel\n\nDies ist **fett**.\n\n- Punkt 1\n- Punkt 2\n"
            try:
                saved = save_markdown_as_docx(md, out, mode="Lesbare DOCX")
                self.assertTrue(saved.exists())
            except ImportError:
                self.skipTest("python-docx nicht installiert")

    def test_proof_docx_with_warnings(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            out    = Path(tmpdir) / "proof.docx"
            dummy  = Path(tmpdir) / "dummy.png"
            dummy.write_bytes(b"fake")
            pages  = {1: "# Seite 1\n| A | B |\n|---|---|\n| 1 | 2 |"}
            report = {"severity": "warning", "warnings": ["Geldbetrag fehlt: 150,00 EUR"]}
            try:
                saved = save_markdown_as_docx(
                    pages, out, mode="Prüf-DOCX",
                    image_paths=[str(dummy)], quality_report=report,
                )
                self.assertTrue(saved.exists())
            except ImportError:
                self.skipTest("python-docx nicht installiert")

    def test_readable_docx_with_quality_report(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            out    = Path(tmpdir) / "warn.docx"
            report = {"severity": "warning", "warnings": ["LA-Code fehlt: 1109"]}
            try:
                saved = save_markdown_as_docx(
                    "# Text\nInhalt.", out, mode="Lesbare DOCX",
                    quality_report=report,
                )
                self.assertTrue(saved.exists())
            except ImportError:
                self.skipTest("python-docx nicht installiert")


from core.cloud.folder_registry import FolderRegistry
from core.cloud.organizer import DocumentOrganizer
from core.cloud.classifier import classify_document

class TestFolderRegistry(unittest.TestCase):

    def test_default_creation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            reg = FolderRegistry(Path(tmpdir))
            self.assertEqual([], reg.get_known_paths())
            self.assertEqual([], reg.get_persons())
            self.assertEqual({}, reg.get_path_contexts())
            self.assertTrue(reg.registry_file.exists())

    def test_add_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            reg = FolderRegistry(Path(tmpdir))
            reg.add_person("Jan")
            reg.add_person("Charlotte")
            self.assertTrue(reg.add_path("Jan/Auto"))
            self.assertFalse(reg.add_path("Jan/Auto"))
            self.assertTrue(reg.add_path("Charlotte/Hobby"))
            self.assertIn("Charlotte/Hobby", reg.get_known_paths())
            self.assertFalse(reg.add_path("NewPerson/Kategorie"))
            self.assertNotIn("NewPerson", reg.get_persons())

    def test_add_path_casing_and_invalid_main(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            reg = FolderRegistry(Path(tmpdir))
            reg.add_person("Jan")
            self.assertTrue(reg.add_path("jan/test"))
            self.assertIn("Jan/test", reg.get_known_paths())
            self.assertFalse(reg.add_path("invalid/path"))
            self.assertNotIn("invalid/path", reg.get_known_paths())

    def test_tree_operations(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            reg = FolderRegistry(Path(tmpdir))
            tree = {
                "Jan": {
                    "Auto": {},
                    "Arbeit": {
                        "Projekte": {}
                    }
                },
                "Laura": {}
            }
            reg.save_tree(tree)
            
            # Check saved structures
            self.assertIn("Jan", reg.get_persons())
            self.assertIn("Laura", reg.get_persons())
            self.assertIn("Jan/Auto", reg.get_known_paths())
            self.assertIn("Jan/Arbeit/Projekte", reg.get_known_paths())
            
            # Retrieve tree and check equality
            loaded_tree = reg.get_tree()
            self.assertEqual(loaded_tree["Jan"]["Auto"], {})
            self.assertEqual(loaded_tree["Jan"]["Arbeit"]["Projekte"], {})
            self.assertEqual(loaded_tree["Laura"], {})

    def test_path_context_roundtrip_and_prune(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            reg = FolderRegistry(Path(tmpdir))
            reg.save_tree({"Jan": {"Auto": {"Golf": {}}, "Hobby": {}}})
            reg.set_path_context("Jan/Auto/Golf", {
                "object_type": "vehicle",
                "aliases": "Golf 7, VW Golf",
                "keywords": ["AB CD 123", "Inspektion", "Inspektion"],
                "notes": "Dienstwagen.",
            })

            reloaded = FolderRegistry(Path(tmpdir))
            context = reloaded.get_path_context("Jan/Auto/Golf")
            self.assertEqual(context["object_type"], "vehicle")
            self.assertEqual(context["aliases"], ["Golf 7", "VW Golf"])
            self.assertEqual(context["keywords"], ["AB CD 123", "Inspektion"])

            reloaded.save_tree({"Jan": {"Hobby": {}}})
            self.assertEqual({}, reloaded.get_path_contexts())


class TestDocumentOrganizer(unittest.TestCase):

    def test_organize_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            final_dir = tmpdir_path / "final"
            final_dir.mkdir()
            
            doc_pdf = final_dir / "2026-05-21_Testdoc_Rechnung.pdf"
            doc_txt = final_dir / "2026-05-21_Testdoc_Rechnung.txt"
            doc_report = final_dir / "2026-05-21_Testdoc_Rechnung_quality_report.json"
            other_file = final_dir / "other_file.pdf"
            
            doc_pdf.write_text("pdf-content", encoding="utf-8")
            doc_txt.write_text("txt-content", encoding="utf-8")
            doc_report.write_text("report-content", encoding="utf-8")
            other_file.write_text("other-content", encoding="utf-8")
            
            organizer = DocumentOrganizer(final_dir)
            moved = organizer.organize("2026-05-21_Testdoc_Rechnung", "Jan/Rechnungen")
            
            target_dir = final_dir / "Jan/Rechnungen"
            self.assertTrue((target_dir / "2026-05-21_Testdoc_Rechnung.pdf").exists())
            self.assertTrue((target_dir / "2026-05-21_Testdoc_Rechnung.txt").exists())
            self.assertFalse((target_dir / "2026-05-21_Testdoc_Rechnung_quality_report.json").exists())
            
            self.assertFalse(doc_pdf.exists())
            self.assertFalse(doc_txt.exists())
            self.assertTrue(doc_report.exists())
            
            self.assertTrue(other_file.exists())


class TestClassifier(unittest.TestCase):

    def test_classify_existing_path(self):
        class MockLLM:
            def __init__(self):
                self.analysis_model = "mock-model"
                self.fusion_model = None
            def query(self, model, system_prompt, user_prompt, think=False, **kwargs):
                return '{"recommended_path": "Jan/Auto", "is_new": false}'
                
        known = ["Jan/Auto", "Sonstiges"]
        res = classify_document("Some fused text about Auto repair", {}, known, MockLLM(), ["Jan", "Sonstiges"])
        self.assertEqual(res["recommended_path"], "Jan/Auto")
        self.assertFalse(res["is_new"])

    def test_classify_new_path(self):
        class MockLLM:
            def __init__(self):
                self.analysis_model = "mock-model"
                self.fusion_model = None
            def query(self, model, system_prompt, user_prompt, think=False, **kwargs):
                return '{"recommended_path": "Jan\\\\Hobby", "is_new": true}'
                
        known = ["Jan/Auto", "Sonstiges"]
        res = classify_document("Some fused text about painting hobby", {}, known, MockLLM(), ["Jan", "Sonstiges"])
        self.assertEqual(res["recommended_path"], "Jan/Hobby")
        self.assertTrue(res["is_new"])


class TestPipelineExport(unittest.TestCase):

    @patch("core.pipeline.inject_fused_text_and_metadata")
    @patch("core.pipeline.save_markdown_as_docx")
    def test_export_respects_settings(self, mock_save_docx, mock_inject_pdf):
        from core.config import AppConfig
        from core.pipeline import PipelineOrchestrator
        from unittest.mock import MagicMock
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            config = AppConfig(tmpdir_path)
            
            config.final_dir.mkdir(parents=True, exist_ok=True)
            
            orch = PipelineOrchestrator(
                config=config,
                llm_client=MagicMock(),
                output_format="PDF und DOCX",
                save_docx_enabled=False,
                save_json_enabled=False,
                gdrive_enabled=False
            )
            
            orch._stage_export(
                work_pdf=Path("dummy.pdf"),
                fused_pages={1: "text"},
                fused_text="text",
                final_name="doc",
                metadata={},
                image_paths=[],
                quality_report={"warnings": []}
            )
            
            docx_file = config.final_dir / "begleitdateien" / "doc.docx"
            json_file = config.final_dir / "begleitdateien" / "doc_quality_report.json"
            self.assertFalse(docx_file.exists())
            self.assertFalse(json_file.exists())
            mock_save_docx.assert_not_called()

    @patch("core.pipeline.inject_fused_text_and_metadata")
    @patch("core.pipeline.save_markdown_as_docx")
    def test_export_generates_for_gdrive_upload(self, mock_save_docx, mock_inject_pdf):
        from core.config import AppConfig
        from core.pipeline import PipelineOrchestrator
        from unittest.mock import MagicMock
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            config = AppConfig(tmpdir_path)
            
            config.final_dir.mkdir(parents=True, exist_ok=True)
            
            orch = PipelineOrchestrator(
                config=config,
                llm_client=MagicMock(),
                output_format="PDF und DOCX",
                save_docx_enabled=False,
                save_json_enabled=False,
                gdrive_enabled=True,
                gdrive_upload_docx=True,
                gdrive_upload_json=True
            )
            
            def side_effect(text, path, **kwargs):
                Path(path).write_text("dummy docx", encoding="utf-8")
                return Path(path)
            mock_save_docx.side_effect = side_effect
            
            orch._stage_export(
                work_pdf=Path("dummy.pdf"),
                fused_pages={1: "text"},
                fused_text="text",
                final_name="doc",
                metadata={},
                image_paths=[],
                quality_report={"warnings": []}
            )
            
            docx_file = config.final_dir / "begleitdateien" / "doc.docx"
            json_file = config.final_dir / "begleitdateien" / "doc_quality_report.json"
            self.assertTrue(json_file.exists())
            self.assertTrue(docx_file.exists())
            mock_save_docx.assert_called_once()

    @patch("core.pipeline.shutil.move")
    def test_process_file_cleanup_after_gdrive_upload(self, mock_move):
        from core.config import AppConfig
        from core.pipeline import PipelineOrchestrator
        from unittest.mock import MagicMock, patch
        
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            config = AppConfig(tmpdir_path)
            
            # Setup folders
            config.base_dir = tmpdir_path
            config.final_dir.mkdir(parents=True, exist_ok=True)
            
            # Setup companion files inside final_dir/begleitdateien
            begleit_dir = config.final_dir / "begleitdateien"
            begleit_dir.mkdir(parents=True, exist_ok=True)
            docx_file = begleit_dir / "doc.docx"
            json_file = begleit_dir / "doc_quality_report.json"
            docx_file.write_text("docx", encoding="utf-8")
            json_file.write_text("json", encoding="utf-8")
            
            # Orchestrator with save_docx=False, save_json=False, gdrive=True
            orch = PipelineOrchestrator(
                config=config,
                llm_client=MagicMock(),
                save_docx_enabled=False,
                save_json_enabled=False,
                gdrive_enabled=True,
                gdrive_upload_docx=True,
                gdrive_upload_json=True
            )
            
            # Mock all stage methods to prevent OCR/LLM executions
            orch._stage_prepare = MagicMock(return_value=Path("dummy_work.pdf"))
            orch._stage_ocrmypdf = MagicMock(return_value=(Path("dummy_ocr.pdf"), "text"))
            orch._stage_docling = MagicMock(return_value=("text", {}))
            orch._stage_extract_pages = MagicMock(return_value=([], {}))
            orch._stage_quality = MagicMock(return_value=("text", {}))
            orch._stage_analysis = MagicMock(return_value=({}, "doc"))
            orch._stage_export = MagicMock()
            orch._stage_organize = MagicMock(return_value=([], ""))
            orch._stage_gdrive_upload = MagicMock()
            
            # Run process_file on a dummy input file
            dummy_input = tmpdir_path / "input.pdf"
            dummy_input.write_text("input", encoding="utf-8")
            
            orch.process_file(dummy_input)
            
            # Google Drive upload should be called with docx and json
            orch._stage_gdrive_upload.assert_called_once()
            
            # Generated companion files should be deleted after upload. The
            # begleitdateien folder remains because it now contains the job manifest.
            self.assertFalse(docx_file.exists())
            self.assertFalse(json_file.exists())
            self.assertTrue((begleit_dir / "doc_job_manifest.json").exists())


class TestPipelineCallbacksAndReview(unittest.TestCase):

    @patch("core.pipeline.shutil.move")
    def test_callbacks_called_and_review_applied(self, mock_move):
        from core.config import AppConfig
        from core.pipeline import PipelineOrchestrator
        
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            config = AppConfig(tmpdir_path)
            config.base_dir = tmpdir_path
            config.final_dir.mkdir(parents=True, exist_ok=True)
            
            from core.cloud.folder_registry import FolderRegistry
            reg = FolderRegistry(tmpdir_path)
            reg.add_person("Jan")
            reg.add_person("Sonstiges")
            
            on_start_mock = MagicMock()
            review_mock = MagicMock(return_value=("edited text", {"date": "2026-05-22", "title": "Reviewed", "document_type": "Befund", "tags": "edited"}, "Reviewed_doc", "Jan/Auto"))
            
            orch = PipelineOrchestrator(
                config=config,
                llm_client=MagicMock(),
                save_docx_enabled=False,
                save_json_enabled=False,
                gdrive_enabled=False,
                review_before_save=True,
                prompt_review_callback=review_mock,
                on_processing_start_callback=on_start_mock
            )
            
            orch._stage_prepare = MagicMock(return_value=Path("dummy_work.pdf"))
            orch._stage_ocrmypdf = MagicMock(return_value=(Path("dummy_ocr.pdf"), "original text"))
            orch._stage_docling = MagicMock(return_value=("original text", {}))
            orch._stage_extract_pages = MagicMock(return_value=([Path("dummy_page1.png")], {1: "original text"}))
            orch._stage_vision_review = MagicMock(return_value=({1: "original text"}, {}))
            orch._stage_fusion = MagicMock(return_value={1: "original text"})
            orch._stage_quality = MagicMock(return_value=("original text", {"warnings": []}))
            orch._stage_analysis = MagicMock(return_value=({"date": "2026-05-22", "title": "Original", "document_type": "Brief", "tags": "org"}, "Original_Brief"))
            orch._stage_export = MagicMock()
            orch._stage_organize = MagicMock(return_value=([], ""))
            
            dummy_input = tmpdir_path / "input.pdf"
            dummy_input.write_text("input", encoding="utf-8")
            
            orch.process_file(dummy_input)
            
            # Check if start callback was called
            on_start_mock.assert_called_once()
            
            # Check if review callback was called with correct parameters
            review_mock.assert_called_once()
            args, kwargs = review_mock.call_args
            # args are: work_pdf, fused_text, metadata, pre_target_path
            self.assertEqual(args[1], "original text")
            self.assertEqual(args[2]["title"], "Original")
            
            # Check if final name and values were updated from review
            # exporting should have been called with updated final_name and fused_text
            orch._stage_export.assert_called_once()
            export_args, export_kwargs = orch._stage_export.call_args
            # fused_text, final_name, metadata
            self.assertEqual(export_args[2], "edited text")
            self.assertEqual(export_args[3], "Reviewed_doc")
            self.assertEqual(export_args[4]["title"], "Reviewed")


class TestPipelineLeakage(unittest.TestCase):

    def test_state_leakage_reset(self):
        from core.config import AppConfig
        from core.pipeline import PipelineOrchestrator
        
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            config = AppConfig(tmpdir_path)
            config.base_dir = tmpdir_path
            config.final_dir.mkdir(parents=True, exist_ok=True)
            
            orch = PipelineOrchestrator(
                config=config,
                llm_client=MagicMock(),
                review_before_save=False,
            )
            
            # Pre-set _chosen_target_path to simulate a leftover manual review decision
            orch._chosen_target_path = "Jan/ManualPath"
            
            orch._stage_prepare = MagicMock(return_value=Path("dummy_work.pdf"))
            orch._stage_ocrmypdf = MagicMock(return_value=(Path("dummy_ocr.pdf"), "original text"))
            orch._stage_docling = MagicMock(return_value=("original text", {}))
            orch._stage_extract_pages = MagicMock(return_value=([], {}))
            orch._stage_quality = MagicMock(return_value=("original text", {"warnings": []}))
            orch._stage_analysis = MagicMock(return_value=({}, "doc"))
            orch._stage_export = MagicMock()
            orch._stage_organize = MagicMock(return_value=([], ""))
            
            dummy_input = tmpdir_path / "input.pdf"
            dummy_input.write_text("input", encoding="utf-8")
            
            # This should reset _chosen_target_path to None immediately at the start of process_file
            orch.process_file(dummy_input)
            self.assertIsNone(orch._chosen_target_path)


class TestWatcherNonBlocking(unittest.TestCase):

    def test_watcher_ticks(self):
        from core.watcher import DirectoryWatcher
        
        # Mock orchestrator and config
        orchestrator = MagicMock()
        orchestrator.config.consume_dir = Path("dummy_consume")
        
        watcher = DirectoryWatcher(orchestrator)
        
        # Mock file_path and stat_info
        file_path = Path("dummy_consume/test.pdf")
        stat_info = MagicMock()
        stat_info.st_size = 100
        stat_info.st_mtime = 1000.0
        
        # Mock Path methods
        with patch.object(Path, "exists", return_value=True), \
             patch.object(Path, "iterdir", return_value=[file_path]), \
             patch.object(Path, "is_file", return_value=True), \
             patch.object(Path, "stat", return_value=stat_info), \
             patch("time.time", return_value=1006.0), \
             patch("time.sleep") as mock_sleep:
            
            # First iteration: file discovered, ticks should be 0
            watcher.is_running = True
            def stop_loop(*args, **kwargs):
                watcher.is_running = False
            mock_sleep.side_effect = stop_loop
            
            watcher._watch_loop()
            
            self.assertIn(file_path, watcher.file_tracker)
            self.assertEqual(watcher.file_tracker[file_path]["last_size"], 100)
            self.assertEqual(watcher.file_tracker[file_path]["stable_ticks"], 0)
            
            # Second iteration: size unchanged, stable_ticks should increment to 1
            watcher.is_running = True
            watcher._watch_loop()
            self.assertEqual(watcher.file_tracker[file_path]["stable_ticks"], 1)
            
            # Third iteration: size unchanged, stable_ticks becomes 2. Age is 1006 - 1000 = 6 seconds (>= 5.0)
            # exclusive check (open) succeeds -> queued
            watcher.is_running = True
            mock_open = patch("builtins.open", MagicMock())
            with mock_open:
                watcher._watch_loop()
            
            # It should be queued and removed from file_tracker, added to seen_files
            self.assertNotIn(file_path, watcher.file_tracker)
            self.assertIn(file_path, watcher.seen_files)
            self.assertEqual(watcher.queue.get_nowait(), file_path)

    def test_watcher_cleanup(self):
        from core.watcher import DirectoryWatcher
        
        orchestrator = MagicMock()
        orchestrator.config.consume_dir = Path("dummy_consume")
        watcher = DirectoryWatcher(orchestrator)
        
        file_path = Path("dummy_consume/test.pdf")
        watcher.file_tracker[file_path] = {
            "last_size": 100,
            "stable_ticks": 1,
            "mtime": 1000.0
        }
        
        # Mock Path methods - return empty list so the file is "gone"
        with patch.object(Path, "exists", return_value=True), \
             patch.object(Path, "iterdir", return_value=[]), \
             patch("time.sleep") as mock_sleep:
            
            watcher.is_running = True
            def stop_loop(*args, **kwargs):
                watcher.is_running = False
            mock_sleep.side_effect = stop_loop
            
            watcher._watch_loop()
            
            # The file was not in the iterdir list, so it should be cleaned up from tracker
            self.assertNotIn(file_path, watcher.file_tracker)


class TestClassifierRules(unittest.TestCase):

    def test_single_level_expansion(self):
        class MockLLM:
            def __init__(self, path):
                self.analysis_model = "mock-model"
                self.fusion_model = None
                self.path = path
            def query(self, *args, **kwargs):
                return f'{{"recommended_path": "{self.path}", "is_new": true}}'
        
        known = ["Sonstiges"]
        # "Jan" -> "Jan/Sonstiges"
        res = classify_document("text", {}, known, MockLLM("Jan"), ["Jan", "Sonstiges"])
        self.assertEqual(res["recommended_path"], "Jan/Sonstiges")
        
        # "Sonstiges" -> remains "Sonstiges"
        res = classify_document("text", {}, known, MockLLM("Sonstiges"), ["Jan", "Sonstiges"])
        self.assertEqual(res["recommended_path"], "Sonstiges")

    def test_deep_path_allows_object_depth_and_guards_extreme_depth(self):
        class MockLLM:
            def __init__(self, path):
                self.analysis_model = "mock-model"
                self.fusion_model = None
                self.path = path
            def query(self, *args, **kwargs):
                return f'{{"recommended_path": "{self.path}", "is_new": true}}'
                
        known = ["Sonstiges"]
        res = classify_document("text", {}, known, MockLLM("Jan/Gesundheit/Zahnarzt/Termine"), ["Jan", "Sonstiges"])
        self.assertEqual(res["recommended_path"], "Jan/Gesundheit/Zahnarzt/Termine")

        res = classify_document("text", {}, known, MockLLM("Jan/A/B/C/D/E"), ["Jan", "Sonstiges"])
        self.assertEqual(res["recommended_path"], "Jan/A/B/C")

    def test_unrecognized_person_fallback(self):
        class MockLLM:
            def __init__(self, path):
                self.analysis_model = "mock-model"
                self.fusion_model = None
                self.path = path
            def query(self, *args, **kwargs):
                return f'{{"recommended_path": "{self.path}", "is_new": true}}'
                
        known = ["Sonstiges"]
        # "Muster/Kategorie" -> "Sonstiges/Kategorie"
        res = classify_document("text", {}, known, MockLLM("Muster/Kategorie"), ["Sonstiges"])
        self.assertEqual(res["recommended_path"], "Sonstiges/Kategorie")

    def test_case_insensitive_exact_match(self):
        class MockLLM:
            def __init__(self, path):
                self.analysis_model = "mock-model"
                self.fusion_model = None
                self.path = path
            def query(self, *args, **kwargs):
                return f'{{"recommended_path": "{self.path}", "is_new": false}}'
                
        known = ["Jan/Gesundheit", "Laura/Finanzen"]
        # Case insensitive match to "Jan/Gesundheit"
        res = classify_document("text", {}, known, MockLLM("jan/gesundheit"), ["Jan", "Laura", "Sonstiges"])
        self.assertEqual(res["recommended_path"], "Jan/Gesundheit")
        self.assertFalse(res["is_new"])

    def test_context_match_prefers_specific_vehicle_path_without_llm(self):
        class MockLLM:
            analysis_model = "mock-model"
            fusion_model = None
            def query(self, *args, **kwargs):
                raise AssertionError("Context match should avoid LLM query")

        known = ["Fabio/Auto/Golf", "Fabio/Auto/Tesla"]
        contexts = {
            "Fabio/Auto/Golf": {
                "object_type": "vehicle",
                "aliases": ["Golf 7"],
                "keywords": ["AB CD 123", "Inspektion"],
            },
            "Fabio/Auto/Tesla": {
                "object_type": "vehicle",
                "aliases": ["Model 3"],
                "keywords": ["EF GH 456"],
            },
        }

        text = "Rechnung fuer Inspektion am VW Golf 7, Kennzeichen AB CD 123."
        res = classify_document(text, {"document_type": "Service"}, known, MockLLM(), ["Fabio"], contexts)

        self.assertEqual(res["recommended_path"], "Fabio/Auto/Golf")
        self.assertEqual(res["reason"], "context_match")
        self.assertFalse(res["is_new"])


class TestAutoSubfolderCreation(unittest.TestCase):

    def test_canonical_doc_type(self):
        from core.pipeline import PipelineOrchestrator
        
        orch = PipelineOrchestrator(
            config=MagicMock(),
            llm_client=MagicMock()
        )
        
        self.assertEqual(orch._get_canonical_doc_type("Entgeldbescheinigung"), "Lohnabrechnung")
        self.assertEqual(orch._get_canonical_doc_type("lohnabrechnung"), "Lohnabrechnung")
        self.assertEqual(orch._get_canonical_doc_type("gehaltsabrechnung"), "Lohnabrechnung")
        self.assertEqual(orch._get_canonical_doc_type("rechnung"), "Rechnung")
        self.assertEqual(orch._get_canonical_doc_type("kaufbeleg"), "Rechnung")
        self.assertEqual(orch._get_canonical_doc_type("arztbrief"), "Befund")
        self.assertEqual(orch._get_canonical_doc_type("Befund"), "Befund")
        self.assertEqual(orch._get_canonical_doc_type("Unbekannt"), "Unbekannt")

    def test_count_existing_documents_with_synonyms(self):
        from core.config import AppConfig
        from core.pipeline import PipelineOrchestrator
        
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            config = AppConfig(tmpdir_path)
            orch = PipelineOrchestrator(config=config, llm_client=MagicMock())
            
            target_dir = tmpdir_path / "Jan/Arbeit"
            target_dir.mkdir(parents=True)
            
            # Create some dummy PDFs with synonym names
            (target_dir / "2026-01-01_Entgeldbescheinigung.pdf").write_bytes(b"%PDF-1.4")
            (target_dir / "2026-02-01_Lohnabrechnung.pdf").write_bytes(b"%PDF-1.4")
            (target_dir / "2026-03-01_Other.pdf").write_bytes(b"%PDF-1.4")
            
            # Count how many of type "Lohnabrechnung"
            count = orch._count_existing_documents_of_type(target_dir, "Lohnabrechnung")
            self.assertEqual(count, 2)

    def test_consolidate_existing_documents(self):
        from core.config import AppConfig
        from core.pipeline import PipelineOrchestrator
        
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            config = AppConfig(tmpdir_path)
            orch = PipelineOrchestrator(config=config, llm_client=MagicMock())
            
            parent_path = "Jan/Arbeit"
            parent_dir = tmpdir_path / "final" / parent_path
            parent_dir.mkdir(parents=True)
            
            # Create files in parent folder
            file1 = parent_dir / "2026-01-01_Entgeldbescheinigung.pdf"
            file2 = parent_dir / "2026-02-01_Lohnabrechnung.pdf"
            file3 = parent_dir / "2026-03-01_Other.pdf"
            file1.write_bytes(b"%PDF-1.4")
            file2.write_bytes(b"%PDF-1.4")
            file3.write_bytes(b"%PDF-1.4")
            
            # Consolidate Lohnabrechnung
            orch._consolidate_existing_documents(parent_path, "Lohnabrechnung")
            
            sub_dir = parent_dir / "Lohnabrechnung"
            self.assertTrue(sub_dir.exists())
            self.assertTrue((sub_dir / "2026-01-01_Entgeldbescheinigung.pdf").exists())
            self.assertTrue((sub_dir / "2026-02-01_Lohnabrechnung.pdf").exists())
            
            # "Other" should remain in parent directory
            self.assertTrue(file3.exists())
            self.assertFalse(file1.exists())
            self.assertFalse(file2.exists())

    def test_stage_organize_auto_subfolder_trigger(self):
        from core.config import AppConfig
        from core.pipeline import PipelineOrchestrator
        
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            config = AppConfig(tmpdir_path)
            
            from core.cloud.folder_registry import FolderRegistry
            reg = FolderRegistry(tmpdir_path)
            reg.add_person("Jan")
            reg.add_person("Sonstiges")
            reg.add_path("Jan/Arbeit")
            
            orch = PipelineOrchestrator(config=config, llm_client=MagicMock())
            
            # Create a file in final/Jan/Arbeit to simulate 1 existing file
            target_dir = tmpdir_path / "final/Jan/Arbeit"
            target_dir.mkdir(parents=True)
            (target_dir / "2026-01-01_Entgeldbescheinigung.pdf").write_bytes(b"%PDF-1.4")
            
            # We process a new file with document_type "Lohnabrechnung" (synonym)
            # LLM returns target path "Jan/Arbeit"
            orch._chosen_target_path = "Jan/Arbeit"
            
            # Create the file in final_dir that needs organizing
            final_name = "2026-02-01_Entgelt_Lohnabrechnung"
            new_file = tmpdir_path / "final" / f"{final_name}.pdf"
            new_file.write_bytes(b"%PDF-1.4")
            
            # Run _stage_organize
            metadata = {"document_type": "Lohnabrechnung"}
            moved_files, target_path = orch._stage_organize("text", metadata, final_name)
            
            # Target path should have been updated to Jan/Arbeit/Lohnabrechnung
            self.assertEqual(target_path, "Jan/Arbeit/Lohnabrechnung")
            
            # New file should be moved to subfolder
            sub_dir = target_dir / "Lohnabrechnung"
            self.assertTrue(sub_dir.exists())
            self.assertTrue((sub_dir / f"{final_name}.pdf").exists())
            
            # Existing file should have been consolidated
            self.assertTrue((sub_dir / "2026-01-01_Entgeldbescheinigung.pdf").exists())


class TestOcrPrep(unittest.TestCase):

    @patch("core.ocr.pdf_prep.subprocess.run")
    @patch("core.ocr.pdf_prep.shutil.which", return_value="/usr/bin/ocrmypdf")
    def test_run_ocrmypdf_includes_rotate_pages(self, mock_which, mock_run):
        from core.ocr.pdf_prep import run_ocrmypdf
        mock_run.return_value = MagicMock(returncode=0)
        
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            sidecar = tmpdir_path / "sidecar.txt"
            
            run_ocrmypdf(Path("input.pdf"), Path("output.pdf"), sidecar)
            
            mock_run.assert_called_once()
            args, kwargs = mock_run.call_args
            cmd = args[0]
            self.assertIn("--rotate-pages", cmd)
            self.assertIn("--rotate-pages-threshold", cmd)
            self.assertIn("7", cmd)
            self.assertIn("--deskew", cmd)
            self.assertIn("--force-ocr", cmd)

    @patch("core.ocr.pdf_prep.subprocess.run")
    @patch("core.ocr.pdf_prep.shutil.which", return_value="/usr/bin/ocrmypdf")
    def test_run_image_to_pdf_includes_rotation_flags(self, mock_which, mock_run):
        from core.ocr.pdf_prep import run_image_to_pdf
        mock_run.return_value = MagicMock(returncode=0)
        
        run_image_to_pdf(Path("input.jpg"), Path("output.pdf"))
        
        mock_run.assert_called_once()
        args, kwargs = mock_run.call_args
        cmd = args[0]
        self.assertIn("--rotate-pages", cmd)
        self.assertIn("--rotate-pages-threshold", cmd)
        self.assertIn("7", cmd)


class TestGDriveConsolidation(unittest.TestCase):

    @patch("core.cloud.gdrive_client.GoogleDriveClient")
    def test_consolidate_existing_documents_gdrive(self, mock_gdrive_class):
        from core.pipeline import PipelineOrchestrator
        from core.config import AppConfig
        
        # Setup mocks
        mock_client = MagicMock()
        mock_gdrive_class.return_value = mock_client
        mock_client.is_authenticated.return_value = True
        
        mock_service = MagicMock()
        mock_client._get_service.return_value = mock_service
        
        # Resolving path returns dummy folder IDs
        def mock_resolve(service, path):
            if path == "Jan/Arbeit":
                return "parent_id_123"
            elif path == "Jan/Arbeit/Lohnabrechnung":
                return "sub_id_456"
            return "root"
        mock_client._resolve_path_to_folder_id.side_effect = mock_resolve
        
        # Mock file listing
        mock_files = [
            {"id": "file_id_789", "name": "2026-01-01_Entgeldbescheinigung.pdf", "parents": ["parent_id_123"]},
            {"id": "file_id_000", "name": "2026-02-01_Lohnabrechnung.pdf", "parents": ["parent_id_123"]},
            {"id": "file_id_other", "name": "2026-03-01_Other.pdf", "parents": ["parent_id_123"]}
        ]
        mock_service.files().list.return_value.execute.return_value = {"files": mock_files}
        
        # Setup Orchestrator
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            config = AppConfig(tmpdir_path)
            orch = PipelineOrchestrator(config=config, llm_client=MagicMock(), gdrive_enabled=True)
            
            # Execute GDrive consolidation
            orch._consolidate_existing_documents_gdrive("Jan/Arbeit", "Lohnabrechnung")
            
            # Assertions:
            # list should have been called
            mock_service.files().list.assert_called_once()
            
            # update should have been called twice (for the two Lohnabrechnung synonyms files)
            self.assertEqual(mock_service.files().update.call_count, 2)
            
            # Check arguments of the first update call
            first_call_args = mock_service.files().update.call_args_list[0]
            kwargs = first_call_args.kwargs
            self.assertEqual(kwargs["fileId"], "file_id_789")
            self.assertEqual(kwargs["addParents"], "sub_id_456")
            self.assertEqual(kwargs["removeParents"], "parent_id_123")
            
            # Check arguments of the second update call
            second_call_args = mock_service.files().update.call_args_list[1]
            kwargs2 = second_call_args.kwargs
            self.assertEqual(kwargs2["fileId"], "file_id_000")
            self.assertEqual(kwargs2["addParents"], "sub_id_456")
            self.assertEqual(kwargs2["removeParents"], "parent_id_123")


import PIL.Image

class TestHEICPrepare(unittest.TestCase):

    @patch("pillow_heif.register_heif_opener")
    @patch("PIL.Image.open")
    def test_heic_conversion_stage_prepare(self, mock_image_open, mock_register):
        from core.pipeline import PipelineOrchestrator
        from core.config import AppConfig
        
        # Setup mock image object
        mock_img = MagicMock()
        mock_img.mode = "RGBA"
        mock_image_open.return_value = mock_img
        
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            config = AppConfig(tmpdir_path)
            orch = PipelineOrchestrator(config=config, llm_client=MagicMock())
            
            original_path = tmpdir_path / "input.heic"
            original_path.write_text("dummy", encoding="utf-8") # Create dummy file
            
            work_dir = tmpdir_path / "work"
            work_dir.mkdir()
            
            work_pdf = orch._stage_prepare(original_path, work_dir)
            
            # Assertions
            mock_register.assert_called_once()
            mock_image_open.assert_called_once_with(original_path)
            mock_img.convert.assert_called_once_with("RGB")
            mock_img.convert.return_value.save.assert_called_once()
            mock_img.convert.return_value.close.assert_called_once()
            self.assertEqual(work_pdf.name, "input_work.pdf")


class TestImageDescriptionPipeline(unittest.TestCase):

    def test_settings_includes_image_description_prompt(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = SettingsManager(Path(tmpdir) / "settings.json")
            s = mgr.load()
            self.assertIn("image_description", s["prompts"])
            self.assertTrue("Bildbeschreibung" in s["prompts"]["image_description"])

    def test_run_image_description_calls_query(self):
        mock_llm = LLMClient(vision_model="vis", fusion_model="fus", analysis_model="ana")
        mock_llm.query = MagicMock(return_value="Das ist ein rotes Auto.")
        
        desc = mock_llm.run_image_description("image.png", 1)
        self.assertEqual(desc, "Das ist ein rotes Auto.")
        mock_llm.query.assert_called_once()
        args, kwargs = mock_llm.query.call_args
        self.assertEqual(args[0], "vis")
        self.assertIn("Bildbeschreibung", args[1])
        self.assertIn("Seite 1", args[2])
        self.assertEqual(args[3], "image.png")

    @patch("core.pipeline.shutil.move")
    def test_pipeline_only_image_page(self, mock_move):
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
            mock_llm.run_image_description.return_value = "Eine reine Skizze eines Hauses."
            
            orch = PipelineOrchestrator(
                config=config,
                llm_client=mock_llm,
                save_docx_enabled=False,
                save_json_enabled=False,
                gdrive_enabled=False
            )
            
            # Setup stage mocks
            orch._stage_prepare = MagicMock(return_value=Path("dummy_work.pdf"))
            orch._stage_ocrmypdf = MagicMock(return_value=(Path("dummy_ocr.pdf"), ""))
            orch._stage_docling = MagicMock(return_value=("", {}))
            orch._stage_extract_pages = MagicMock(return_value=([Path("dummy_page1.png")], {1: ""}))
            orch._stage_quality = MagicMock(side_effect=lambda o, d, v, f: (f, {"warnings": []}))
            orch._stage_analysis = MagicMock(return_value=({}, "doc"))
            orch._stage_export = MagicMock()
            orch._stage_organize = MagicMock(return_value=([], ""))
            
            dummy_input = tmpdir_path / "input.pdf"
            dummy_input.write_text("input", encoding="utf-8")
            
            orch.process_file(dummy_input)
            
            # Verify image description was called
            mock_llm.run_image_description.assert_called_once_with("dummy_page1.png", 1)
            # Normal vision_review should NOT be called since text length is 0
            mock_llm.run_vision_review.assert_not_called()
            
            # Verify export was called with image description in fused_pages and fused_text
            orch._stage_export.assert_called_once()
            args = orch._stage_export.call_args[0]
            # args: work_pdf, fused_pages, fused_text, final_name, metadata, image_paths, quality_report
            self.assertEqual(args[1], {1: "[Bildbeschreibung: Eine reine Skizze eines Hauses.]"})
            self.assertEqual(args[2], "[Bildbeschreibung: Eine reine Skizze eines Hauses.]")

    @patch("core.pipeline.shutil.move")
    def test_pipeline_low_text_page(self, mock_move):
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
            mock_llm.run_image_description.return_value = "Ein Stempel mit der Aufschrift 'Bezahlt'."
            mock_llm.run_vision_review.return_value = "Bezahlt"
            mock_llm.run_page_fusion.return_value = "Bezahlt"
            
            orch = PipelineOrchestrator(
                config=config,
                llm_client=mock_llm,
                save_docx_enabled=False,
                save_json_enabled=False,
                gdrive_enabled=False
            )
            
            # Setup stage mocks
            orch._stage_prepare = MagicMock(return_value=Path("dummy_work.pdf"))
            # 40 chars of text (under 150 threshold)
            orch._stage_ocrmypdf = MagicMock(return_value=(Path("dummy_ocr.pdf"), "Stempel: Bezahlt"))
            orch._stage_docling = MagicMock(return_value=("Stempel: Bezahlt", {1: "Stempel: Bezahlt"}))
            orch._stage_extract_pages = MagicMock(return_value=([Path("dummy_page1.png")], {1: "Stempel: Bezahlt"}))
            orch._stage_quality = MagicMock(side_effect=lambda o, d, v, f: (f, {"warnings": []}))
            orch._stage_analysis = MagicMock(return_value=({}, "doc"))
            orch._stage_export = MagicMock()
            orch._stage_organize = MagicMock(return_value=([], ""))
            
            dummy_input = tmpdir_path / "input.pdf"
            dummy_input.write_text("input", encoding="utf-8")
            
            orch.process_file(dummy_input)
            
            # Verify both run_image_description AND run_vision_review were called (Mischform)
            mock_llm.run_image_description.assert_called_once_with("dummy_page1.png", 1)
            mock_llm.run_vision_review.assert_called_once_with("dummy_page1.png", "Stempel: Bezahlt", 1)
            
            # Verify description is prepended in exporting
            orch._stage_export.assert_called_once()
            args = orch._stage_export.call_args[0]
            self.assertEqual(args[1], {1: "[Bildbeschreibung: Ein Stempel mit der Aufschrift 'Bezahlt'.]\n\nBezahlt"})
            self.assertEqual(args[2], "[Bildbeschreibung: Ein Stempel mit der Aufschrift 'Bezahlt'.]\n\nBezahlt")

    @patch("core.pipeline.shutil.move")
    def test_pipeline_normal_text_page(self, mock_move):
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
            mock_llm.run_vision_review.return_value = "normaler text" * 30
            mock_llm.run_page_fusion.return_value = "normaler text" * 30
            
            orch = PipelineOrchestrator(
                config=config,
                llm_client=mock_llm,
                save_docx_enabled=False,
                save_json_enabled=False,
                gdrive_enabled=False
            )
            
            long_text = "Dies ist ein sehr langer Text, der die Grenze von 150 Zeichen deutlich ueberschreitet. " * 5
            orch._stage_prepare = MagicMock(return_value=Path("dummy_work.pdf"))
            orch._stage_ocrmypdf = MagicMock(return_value=(Path("dummy_ocr.pdf"), long_text))
            orch._stage_docling = MagicMock(return_value=(long_text, {1: long_text}))
            orch._stage_extract_pages = MagicMock(return_value=([Path("dummy_page1.png")], {1: long_text}))
            orch._stage_quality = MagicMock(side_effect=lambda o, d, v, f: (f, {"warnings": []}))
            orch._stage_analysis = MagicMock(return_value=({}, "doc"))
            orch._stage_export = MagicMock()
            orch._stage_organize = MagicMock(return_value=([], ""))
            
            dummy_input = tmpdir_path / "input.pdf"
            dummy_input.write_text("input", encoding="utf-8")
            
            orch.process_file(dummy_input)
            
            mock_llm.run_image_description.assert_not_called()
            # normal vision review should be called
            mock_llm.run_vision_review.assert_called_once()

    @patch("core.pipeline.shutil.move")
    def test_pipeline_preserves_page_by_page_mapping(self, mock_move):
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
            mock_llm.run_vision_review.side_effect = lambda path, md, num: f"corrected page {num}"
            mock_llm.run_page_fusion.side_effect = lambda **kwargs: f"fused page {kwargs.get('page_num')}"
            
            orch = PipelineOrchestrator(
                config=config,
                llm_client=mock_llm,
                save_docx_enabled=False,
                save_json_enabled=False,
                gdrive_enabled=False
            )
            
            # 2 pages
            long_text = "Dies ist ein sehr langer Text, der die Grenze von 150 Zeichen deutlich ueberschreitet. " * 5
            orch._stage_prepare = MagicMock(return_value=Path("dummy_work.pdf"))
            orch._stage_ocrmypdf = MagicMock(return_value=(Path("dummy_ocr.pdf"), long_text))
            orch._stage_docling = MagicMock(return_value=(long_text, {1: long_text, 2: long_text}))
            orch._stage_extract_pages = MagicMock(return_value=([Path("p1.png"), Path("p2.png")], {1: long_text, 2: long_text}))
            
            # Scenario A: No Quality correction -> preserves page-by-page mapping
            orch._stage_quality = MagicMock(side_effect=lambda o, d, v, f: (f, {"warnings": []}))
            orch._stage_analysis = MagicMock(return_value=({}, "doc"))
            orch._stage_export = MagicMock()
            orch._stage_organize = MagicMock(return_value=([], ""))
            
            dummy_input = tmpdir_path / "input.pdf"
            dummy_input.write_text("input", encoding="utf-8")
            
            orch.process_file(dummy_input)
            
            # Export args: fused_pages is args[1]
            orch._stage_export.assert_called_once()
            args = orch._stage_export.call_args[0]
            self.assertEqual(args[1], {1: "fused page 1", 2: "fused page 2"})
            
            # Scenario B: Quality correction happens -> PDF overlay remains page-by-page.
            orch._stage_export.reset_mock()
            orch._stage_quality = MagicMock(side_effect=lambda o, d, v, f: ("entirely corrected text", {"warnings": []}))
            
            # Reset dummy input
            dummy_input.write_text("input", encoding="utf-8")
            orch.process_file(dummy_input)
            
            orch._stage_export.assert_called_once()
            args = orch._stage_export.call_args[0]
            self.assertEqual(args[1], {1: "fused page 1", 2: "fused page 2"})
            self.assertEqual(args[2], "entirely corrected text")


class TestLargePdfMode(unittest.TestCase):

    @patch("core.pipeline.shutil.move")
    @patch("fitz.open")
    def test_reduced_analysis_for_large_pdf(self, mock_fitz_open, mock_move):
        from core.config import AppConfig
        from core.pipeline import PipelineOrchestrator
        
        # Mock fitz document
        mock_doc = MagicMock()
        mock_doc.__len__.return_value = 25
        mock_fitz_open.return_value = mock_doc
        
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            config = AppConfig(tmpdir_path)
            config.base_dir = tmpdir_path
            config.final_dir.mkdir(parents=True, exist_ok=True)
            
            mock_llm = MagicMock()
            mock_llm.vision_model = "vision-model"
            mock_llm.glm_ocr_model = "Keins"
            
            orch = PipelineOrchestrator(
                config=config,
                llm_client=mock_llm,
                large_pdf_reduced=True,
                save_docx_enabled=False,
                save_json_enabled=False,
                gdrive_enabled=False
            )
            
            dummy_ocr_pdf = tmpdir_path / "dummy_ocr.pdf"
            dummy_ocr_pdf.write_text("ocr content", encoding="utf-8")
            
            orch._stage_prepare = MagicMock(return_value=Path("dummy_work.pdf"))
            orch._stage_ocrmypdf = MagicMock(return_value=(dummy_ocr_pdf, "raw ocr text"))
            
            # Non-reduced stages that should be bypassed
            orch._stage_docling = MagicMock()
            orch._stage_extract_pages = MagicMock()
            orch._stage_glm_ocr = MagicMock()
            orch._stage_vision_review = MagicMock()
            orch._stage_fusion = MagicMock()
            orch._stage_quality = MagicMock()
            
            # Reduced analysis still runs analysis and export
            orch._stage_analysis = MagicMock(return_value=({"title": "LargeDoc"}, "doc_output"))
            orch._stage_export = MagicMock()
            orch._stage_organize = MagicMock(return_value=([], ""))
            
            dummy_input = tmpdir_path / "input.pdf"
            dummy_input.write_text("input", encoding="utf-8")
            
            orch.process_file(dummy_input)
            
            # Assertions
            orch._stage_docling.assert_not_called()
            orch._stage_extract_pages.assert_not_called()
            orch._stage_glm_ocr.assert_not_called()
            orch._stage_vision_review.assert_not_called()
            orch._stage_fusion.assert_not_called()
            orch._stage_quality.assert_not_called()
            
            orch._stage_analysis.assert_called_once_with("raw ocr text")
            orch._stage_export.assert_called_once()
            
            # Check arguments of export:
            # def _stage_export(self, work_pdf, fused_pages, fused_text, final_name, metadata, image_paths, quality_report)
            call_args = orch._stage_export.call_args[0]
            self.assertEqual(call_args[0], dummy_ocr_pdf)
            self.assertEqual(call_args[1], {})
            self.assertEqual(call_args[2], "raw ocr text")

    @patch("core.pipeline.shutil.move")
    @patch("fitz.open")
    def test_extended_analysis_for_large_pdf(self, mock_fitz_open, mock_move):
        from core.config import AppConfig
        from core.pipeline import PipelineOrchestrator
        
        # Mock fitz document to 25 pages
        mock_doc = MagicMock()
        mock_doc.__len__.return_value = 25
        mock_fitz_open.return_value = mock_doc
        
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            config = AppConfig(tmpdir_path)
            config.base_dir = tmpdir_path
            config.final_dir.mkdir(parents=True, exist_ok=True)
            
            mock_llm = MagicMock()
            mock_llm.vision_model = "vision-model"
            mock_llm.glm_ocr_model = "Keins"
            
            orch = PipelineOrchestrator(
                config=config,
                llm_client=mock_llm,
                large_pdf_reduced=False,  # EXTENDED analysis
                save_docx_enabled=False,
                save_json_enabled=False,
                gdrive_enabled=False
            )
            
            dummy_work_pdf = tmpdir_path / "dummy_work.pdf"
            dummy_work_pdf.write_text("work", encoding="utf-8")
            dummy_ocr_pdf = tmpdir_path / "dummy_ocr.pdf"
            dummy_ocr_pdf.write_text("ocr content", encoding="utf-8")
            
            orch._stage_prepare = MagicMock(return_value=dummy_work_pdf)
            orch._stage_ocrmypdf = MagicMock(return_value=(dummy_ocr_pdf, "raw ocr text"))
            
            # Standard stages
            orch._stage_docling = MagicMock(return_value=("docling text", {1: "page 1 docling"}))
            orch._stage_extract_pages = MagicMock(return_value=([Path("p1.png")], {1: "page 1 ocr"}))
            orch._stage_glm_ocr = MagicMock(return_value={})
            orch._stage_vision_review = MagicMock(return_value=({1: "page 1 vision"}, {}))
            orch._stage_fusion = MagicMock(return_value={1: "fused page 1"})
            orch._stage_quality = MagicMock(return_value=("fused page 1", {"warnings": []}))
            orch._stage_analysis = MagicMock(return_value=({"title": "LargeDoc"}, "doc_output"))
            orch._stage_export = MagicMock()
            orch._stage_organize = MagicMock(return_value=([], ""))
            
            dummy_input = tmpdir_path / "input.pdf"
            dummy_input.write_text("input", encoding="utf-8")
            
            orch.process_file(dummy_input)
            
            # Assertions: all stages are called
            orch._stage_docling.assert_called_once()
            orch._stage_extract_pages.assert_called_once()
            orch._stage_fusion.assert_called_once()
            orch._stage_quality.assert_called_once()
            
            orch._stage_export.assert_called_once()
            call_args = orch._stage_export.call_args[0]
            # Detailed PDF export should use the OCRmyPDF output when available.
            self.assertEqual(call_args[0], dummy_ocr_pdf)
            self.assertEqual(call_args[1], {1: "fused page 1"})

    @patch("core.pipeline.shutil.move")
    @patch("fitz.open")
    def test_normal_pdf_ignores_reduced_flag(self, mock_fitz_open, mock_move):
        from core.config import AppConfig
        from core.pipeline import PipelineOrchestrator
        
        # Mock fitz document to 5 pages (< 20)
        mock_doc = MagicMock()
        mock_doc.__len__.return_value = 5
        mock_fitz_open.return_value = mock_doc
        
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            config = AppConfig(tmpdir_path)
            config.base_dir = tmpdir_path
            config.final_dir.mkdir(parents=True, exist_ok=True)
            
            mock_llm = MagicMock()
            mock_llm.vision_model = "vision-model"
            mock_llm.glm_ocr_model = "Keins"
            
            orch = PipelineOrchestrator(
                config=config,
                llm_client=mock_llm,
                large_pdf_reduced=True,  # Should be ignored because total pages <= 20
                save_docx_enabled=False,
                save_json_enabled=False,
                gdrive_enabled=False
            )
            
            dummy_work_pdf = tmpdir_path / "dummy_work.pdf"
            dummy_work_pdf.write_text("work", encoding="utf-8")
            dummy_ocr_pdf = tmpdir_path / "dummy_ocr.pdf"
            dummy_ocr_pdf.write_text("ocr content", encoding="utf-8")
            
            orch._stage_prepare = MagicMock(return_value=dummy_work_pdf)
            orch._stage_ocrmypdf = MagicMock(return_value=(dummy_ocr_pdf, "raw ocr text"))
            
            orch._stage_docling = MagicMock(return_value=("docling text", {1: "page 1 docling"}))
            orch._stage_extract_pages = MagicMock(return_value=([Path("p1.png")], {1: "page 1 ocr"}))
            orch._stage_glm_ocr = MagicMock(return_value={})
            orch._stage_vision_review = MagicMock(return_value=({1: "page 1 vision"}, {}))
            orch._stage_fusion = MagicMock(return_value={1: "fused page 1"})
            orch._stage_quality = MagicMock(return_value=("fused page 1", {"warnings": []}))
            orch._stage_analysis = MagicMock(return_value=({"title": "NormalDoc"}, "doc_output"))
            orch._stage_export = MagicMock()
            orch._stage_organize = MagicMock(return_value=([], ""))
            
            dummy_input = tmpdir_path / "input.pdf"
            dummy_input.write_text("input", encoding="utf-8")
            
            orch.process_file(dummy_input)
            
            # Assertions: all stages are called
            orch._stage_docling.assert_called_once()
            orch._stage_extract_pages.assert_called_once()
            orch._stage_export.assert_called_once()
            call_args = orch._stage_export.call_args[0]
            self.assertEqual(call_args[0], dummy_ocr_pdf)


class TestDocxInputMode(unittest.TestCase):

    @patch("core.pipeline.shutil.move")
    def test_docx_input_bypass_ocr_and_page_stages(self, mock_move):
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
            
            orch = PipelineOrchestrator(
                config=config,
                llm_client=mock_llm,
                save_docx_enabled=False,
                save_json_enabled=False,
                gdrive_enabled=False
            )
            
            # Mock the docx text extraction method
            orch._extract_text_from_docx = MagicMock(return_value="extracted docx content")
            
            # Stages to be bypassed
            orch._stage_prepare = MagicMock()
            orch._stage_ocrmypdf = MagicMock()
            orch._stage_docling = MagicMock()
            orch._stage_extract_pages = MagicMock()
            orch._stage_glm_ocr = MagicMock()
            orch._stage_vision_review = MagicMock()
            orch._stage_fusion = MagicMock()
            orch._stage_quality = MagicMock()
            
            # Stages that should run
            orch._stage_analysis = MagicMock(return_value=({"title": "DocxDoc"}, "docx_output"))
            orch._stage_export = MagicMock()
            orch._stage_organize = MagicMock(return_value=([], ""))
            
            # Create a dummy .docx input file
            dummy_input = tmpdir_path / "input.docx"
            dummy_input.write_text("docx", encoding="utf-8")
            
            orch.process_file(dummy_input)
            
            # Verify bypasses
            orch._stage_prepare.assert_not_called()
            orch._stage_ocrmypdf.assert_not_called()
            orch._stage_docling.assert_not_called()
            orch._stage_extract_pages.assert_not_called()
            orch._stage_glm_ocr.assert_not_called()
            orch._stage_vision_review.assert_not_called()
            orch._stage_fusion.assert_not_called()
            orch._stage_quality.assert_not_called()
            
            # Verify execution of analysis and export
            orch._extract_text_from_docx.assert_called_once()
            # Verify export arguments: is_docx must be True
            call_kwargs = orch._stage_export.call_args[1]
            self.assertTrue(call_kwargs.get("is_docx"))

    @patch("docx.Document")
    def test_extract_text_from_docx(self, mock_doc_class):
        from core.pipeline import PipelineOrchestrator
        
        # Setup mock docx structure
        mock_doc = MagicMock()
        
        mock_p1 = MagicMock()
        mock_p1.text = "Hello World"
        mock_p2 = MagicMock()
        mock_p2.text = "Second Paragraph"
        mock_doc.paragraphs = [mock_p1, mock_p2]
        
        # Table
        mock_table = MagicMock()
        mock_cell1 = MagicMock()
        mock_cell1.text = "Cell1"
        mock_cell2 = MagicMock()
        mock_cell2.text = "Cell2"
        mock_row = MagicMock()
        mock_row.cells = [mock_cell1, mock_cell2]
        mock_table.rows = [mock_row]
        mock_doc.tables = [mock_table]
        
        mock_doc_class.return_value = mock_doc
        
        orch = PipelineOrchestrator(config=MagicMock(), llm_client=MagicMock())
        extracted = orch._extract_text_from_docx(Path("dummy.docx"))
        
        self.assertIn("Hello World", extracted)
        self.assertIn("Second Paragraph", extracted)
        self.assertIn("Cell1 | Cell2", extracted)

    @patch("zipfile.ZipFile")
    def test_extract_text_from_odt(self, mock_zipfile):
        from core.pipeline import PipelineOrchestrator
        
        xml_content = b"""<?xml version="1.0" encoding="UTF-8"?>
        <office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0">
            <office:body>
                <office:text>
                    <text:p>Hello ODT World</text:p>
                    <table:table>
                        <table:table-row>
                            <table:table-cell><text:p>CellA</text:p></table:table-cell>
                            <table:table-cell><text:p>CellB</text:p></table:table-cell>
                        </table:table-row>
                    </table:table>
                </office:text>
            </office:body>
        </office:document-content>
        """
        mock_zip = MagicMock()
        mock_zip.read.return_value = xml_content
        mock_zipfile.return_value.__enter__.return_value = mock_zip
        
        orch = PipelineOrchestrator(config=MagicMock(), llm_client=MagicMock())
        extracted = orch._extract_text_from_odt(Path("dummy.odt"))
        
        self.assertIn("Hello ODT World", extracted)
        self.assertIn("CellA | CellB", extracted)

    def test_extract_text_from_odoc(self):
        from core.pipeline import PipelineOrchestrator
        
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "test.odoc"
            path.write_text(json.dumps({
                "title": "My Synology Document",
                "url": "https://synology.nas/drive/123",
                "doc_id": "doc_abc_123"
            }), encoding="utf-8")
            
            orch = PipelineOrchestrator(config=MagicMock(), llm_client=MagicMock())
            extracted = orch._extract_text_from_odoc(path)
            
            self.assertIn("Synology Office Dokument-Link", extracted)
            self.assertIn("Titel: My Synology Document", extracted)
            self.assertIn("URL: https://synology.nas/drive/123", extracted)
            self.assertIn("Dokument-ID: doc_abc_123", extracted)

    def test_extract_text_from_doc_fallback(self):
        from core.pipeline import PipelineOrchestrator
        
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "test.doc"
            text = "My DOC Text content here"
            encoded_utf16 = text.encode("utf-16le")
            binary_content = b"\x00\x01\x02\x03" + encoded_utf16 + b"\x00\x01\x02\x03"
            path.write_bytes(binary_content)
            
            orch = PipelineOrchestrator(config=MagicMock(), llm_client=MagicMock())
            with patch("win32com.client.Dispatch", side_effect=Exception("No Office installed")):
                extracted = orch._extract_text_from_doc(path)
                
            self.assertIn("My DOC Text content here", extracted)

    @patch("core.pipeline.shutil.move")
    def test_odt_input_bypass_ocr_and_page_stages(self, mock_move):
        from core.config import AppConfig
        from core.pipeline import PipelineOrchestrator
        
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            config = AppConfig(tmpdir_path)
            config.base_dir = tmpdir_path
            config.final_dir.mkdir(parents=True, exist_ok=True)
            
            mock_llm = MagicMock()
            orch = PipelineOrchestrator(config=config, llm_client=mock_llm, save_docx_enabled=False, save_json_enabled=False, gdrive_enabled=False)
            
            orch._extract_text_from_odt = MagicMock(return_value="extracted odt text")
            
            orch._stage_prepare = MagicMock()
            orch._stage_ocrmypdf = MagicMock()
            orch._stage_docling = MagicMock()
            orch._stage_extract_pages = MagicMock()
            orch._stage_glm_ocr = MagicMock()
            orch._stage_vision_review = MagicMock()
            orch._stage_fusion = MagicMock()
            orch._stage_quality = MagicMock()
            
            orch._stage_analysis = MagicMock(return_value=({"title": "OdtDoc"}, "odt_output"))
            orch._stage_export = MagicMock()
            orch._stage_organize = MagicMock(return_value=([], ""))
            
            dummy_input = tmpdir_path / "input.odt"
            dummy_input.write_text("odt", encoding="utf-8")
            
            orch.process_file(dummy_input)
            
            orch._stage_prepare.assert_not_called()
            orch._stage_ocrmypdf.assert_not_called()
            orch._extract_text_from_odt.assert_called_once()
            orch._stage_analysis.assert_called_once_with("extracted odt text")
            orch._stage_export.assert_called_once()
            
            call_kwargs = orch._stage_export.call_args[1]
            self.assertTrue(call_kwargs.get("is_docx"))

    @patch("core.pipeline.save_markdown_as_docx")
    def test_odt_stage_export_converts_to_docx(self, mock_save_docx):
        from core.pipeline import PipelineOrchestrator
        from core.config import AppConfig
        
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            config = AppConfig(tmpdir_path)
            config.final_dir.mkdir(parents=True, exist_ok=True)
            
            orch = PipelineOrchestrator(
                config=config,
                llm_client=MagicMock(),
                save_docx_enabled=True,
                gdrive_enabled=False
            )
            
            dummy_odt = tmpdir_path / "dummy_work.odt"
            dummy_odt.write_text("dummy")
            
            orch._stage_export(
                work_pdf=dummy_odt,
                fused_pages={},
                fused_text="fused text content",
                final_name="fused_odt",
                metadata={},
                image_paths=[],
                quality_report={"warnings": []},
                is_docx=True
            )
            
            mock_save_docx.assert_called_once()
            args, kwargs = mock_save_docx.call_args
            self.assertEqual(args[0], "fused text content")
            self.assertEqual(args[1], config.final_dir / "fused_odt.docx")

    def test_stage_organize_defers_when_new_path(self):
        from core.config import AppConfig
        from core.pipeline import PipelineOrchestrator
        
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            config = AppConfig(tmpdir_path)
            config.base_dir = tmpdir_path
            config.final_dir.mkdir(parents=True, exist_ok=True)
            
            from core.cloud.folder_registry import FolderRegistry
            reg = FolderRegistry(tmpdir_path)
            reg.add_person("Jan")
            reg.add_person("Sonstiges")
            
            # Setup prompt callback
            prompt_mock = MagicMock(return_value="Jan/BrandNewFolder")
            
            mock_llm = MagicMock()
            mock_llm.run_classification.return_value = {"recommended_path": "Jan/BrandNewFolder", "is_new": True}
            
            orch = PipelineOrchestrator(
                config=config,
                llm_client=mock_llm,
                prompt_new_folder_callback=prompt_mock,
                save_docx_enabled=False,
                save_json_enabled=False,
                gdrive_enabled=False
            )
            
            # Create a file that needs organizing
            final_name = "2026-05-23_Test_Doc"
            dummy_file = config.final_dir / f"{final_name}.pdf"
            dummy_file.write_text("pdf-content", encoding="utf-8")
            
            # Run organize: it should NOT call the callback, but move the file to staging
            moved_files, target_path = orch._stage_organize("text", {}, final_name)
            
            # The prompt mock should NOT have been called yet!
            prompt_mock.assert_not_called()
            
            # Staging folder should contain the file
            staging_dir = config.final_dir / "_staging" / final_name
            self.assertTrue((staging_dir / f"{final_name}.pdf").exists())
            self.assertEqual(len(orch.deferred_organizations), 1)
            
            # Now run the deferred organization
            orch.process_deferred_organizations()
            
            # Now the prompt mock should be called!
            prompt_mock.assert_called_once_with("Jan/BrandNewFolder")
            
            # Staging folder should be cleaned up / empty
            self.assertFalse(staging_dir.exists())
            
            # Verify the file is now in the final folder
            final_dest = config.final_dir / "Jan/BrandNewFolder" / f"{final_name}.pdf"
            self.assertTrue(final_dest.exists())
            self.assertEqual(final_dest.read_text(encoding="utf-8"), "pdf-content")


class TestPDFPreviewFrame(unittest.TestCase):
    def test_preview_transitions(self):
        import customtkinter as ctk
        import fitz
        from unified_ocr_app.app import PDFPreviewFrame
        
        try:
            root = ctk.CTk()
        except Exception as e:
            raise unittest.SkipTest(f"Skipping GUI test because CTk cannot be initialized: {e}")
            
        try:
            preview = PDFPreviewFrame(root)
            
            with tempfile.TemporaryDirectory() as tmpdir:
                tmpdir_path = Path(tmpdir)
                pdf_path = tmpdir_path / "test_temp.pdf"
                
                # Create a simple PDF using PyMuPDF
                doc = fitz.open()
                page = doc.new_page()
                page.draw_rect(fitz.Rect(10, 10, 50, 50), color=(1, 0, 0), fill=(0, 1, 0))
                doc.save(str(pdf_path))
                doc.close()
                
                docx_path = tmpdir_path / "test_temp.docx"
                docx_path.write_text("dummy", encoding="utf-8")
                
                # 1. Load PDF
                preview.load_pdf(str(pdf_path))
                self.assertIsNotNone(preview.doc)
                self.assertEqual(preview.image_label.cget("text"), "")
                self.assertIsNotNone(preview.image_label.cget("image"))
                
                # 2. Load DOCX (bypass format)
                preview.load_pdf(str(docx_path))
                self.assertIsNone(preview.doc)
                self.assertIn("DOCX-Dokument geladen", preview.image_label.cget("text"))
                self.assertIsNone(preview.image_label.cget("image"))
                
                # 3. Load PDF again (Verify visual preview is restored)
                preview.load_pdf(str(pdf_path))
                self.assertIsNotNone(preview.doc)
                self.assertEqual(preview.image_label.cget("text"), "")
                self.assertIsNotNone(preview.image_label.cget("image"))
                
                # Clean up
                if preview.doc:
                    preview.doc.close()
        finally:
            root.destroy()


if __name__ == "__main__":
    unittest.main()
