import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from core.cloud.folder_registry import (
    FolderRegistry,
    RegistryWriteError,
    UnsafeArchivePath,
    normalize_archive_path,
)
from core.cloud.organizer import DocumentOrganizer, PackageMoveError
from core.local_store import LocalStore, SCHEMA_VERSION


class TestArchivePathSafety(unittest.TestCase):
    def test_traversal_absolute_and_windows_unsafe_paths_are_rejected(self):
        unsafe_paths = (
            "../outside",
            "Jan/../../outside",
            "/absolute/path",
            r"C:\absolute\path",
            r"\\server\share",
            "Jan/CON",
            "Jan/NUL.txt",
            "Jan/bad:name",
            "Jan/bad?name",
            "Jan/trailing.",
        )
        for value in unsafe_paths:
            with self.subTest(value=value):
                with self.assertRaises(UnsafeArchivePath):
                    normalize_archive_path(value)

        self.assertEqual("Jan/Auto/Rechnungen", normalize_archive_path(r" Jan\Auto/Rechnungen "))

    def test_explicit_organizer_api_rejects_traversal_before_mkdir_or_move(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            final_dir = root / "final"
            final_dir.mkdir()
            artifact = final_dir / "document.pdf"
            artifact.write_text("original", encoding="utf-8")

            organizer = DocumentOrganizer(final_dir)
            with self.assertRaises(UnsafeArchivePath):
                organizer.organize_artifacts([artifact], "../outside", package_id="job-1")

            self.assertTrue(artifact.exists())
            self.assertFalse((root / "outside").exists())
            self.assertEqual("target_path_rejected", organizer.last_audit[-1]["action"])


class TestDocumentPackageTransactions(unittest.TestCase):
    def test_legacy_scan_does_not_claim_a_foreign_prefix_match(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            final_dir = Path(tmpdir) / "final"
            final_dir.mkdir()
            intended = final_dir / "doc.pdf"
            foreign = final_dir / "doc_previous.pdf"
            intended.write_text("intended", encoding="utf-8")
            foreign.write_text("foreign", encoding="utf-8")

            moved = DocumentOrganizer(final_dir).organize("doc", "Jan/Archiv")

            self.assertEqual([final_dir / "Jan" / "Archiv" / "doc.pdf"], moved)
            self.assertTrue(foreign.exists())
            self.assertEqual("foreign", foreign.read_text(encoding="utf-8"))

    def test_explicit_package_rolls_every_artifact_back_on_publish_failure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            final_dir = Path(tmpdir) / "final"
            companion_dir = final_dir / "begleitdateien"
            companion_dir.mkdir(parents=True)
            pdf = final_dir / "packet.pdf"
            report = companion_dir / "packet_quality.json"
            pdf.write_text("pdf", encoding="utf-8")
            report.write_text("quality", encoding="utf-8")

            organizer = DocumentOrganizer(final_dir)
            publish = organizer._publish_without_overwrite
            calls = 0

            def fail_second_publish(staged, destination):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("simulated publish failure")
                return publish(staged, destination)

            with patch.object(organizer, "_publish_without_overwrite", side_effect=fail_second_publish):
                with self.assertRaises(PackageMoveError):
                    organizer.organize_artifacts(
                        {"pdf": pdf, "quality": report},
                        "Jan/Archiv",
                        package_id="job-rollback",
                    )

            self.assertEqual("pdf", pdf.read_text(encoding="utf-8"))
            self.assertEqual("quality", report.read_text(encoding="utf-8"))
            target_dir = final_dir / "Jan" / "Archiv"
            self.assertFalse((target_dir / pdf.name).exists())
            self.assertFalse((target_dir / report.name).exists())
            self.assertTrue(any(entry["action"] == "package_rolled_back" for entry in organizer.last_audit))

    def test_one_collision_renames_complete_package_with_shared_stem(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            final_dir = Path(tmpdir) / "final"
            staging = final_dir / "_staging" / "job-new"
            target = final_dir / "Jan" / "Archiv"
            staging.mkdir(parents=True)
            target.mkdir(parents=True)
            stem = "2026-07-12_Acme_Rechnung"
            artifacts = {
                "pdf": staging / f"{stem}.pdf",
                "txt": staging / f"{stem}.txt",
                "quality": staging / f"{stem}_quality_report.json",
                "manifest": staging / f"{stem}_job_manifest.json",
            }
            for role, path in artifacts.items():
                path.write_text(f"new-{role}", encoding="utf-8")
            # The PDF is an identical existing member while only the TXT is a
            # real collision. Even so, the new package must not reuse the old
            # PDF and put only its TXT under a conflict stem.
            existing_pdf = target / f"{stem}.pdf"
            existing_pdf.write_text("new-pdf", encoding="utf-8")
            existing_txt = target / f"{stem}.txt"
            existing_txt.write_text("existing", encoding="utf-8")

            organizer = DocumentOrganizer(final_dir)
            with patch.object(organizer, "_timestamp", return_value="20260712_120000_000001"):
                moved = organizer.organize_artifacts(
                    artifacts,
                    "Jan/Archiv",
                    package_id="job-new",
                )

            conflict_stem = f"{stem}_conflict_20260712_120000_000001"
            expected = [
                target / f"{conflict_stem}.pdf",
                target / f"{conflict_stem}.txt",
                target / f"{conflict_stem}_quality_report.json",
                target / f"{conflict_stem}_job_manifest.json",
            ]
            self.assertEqual(expected, moved)
            self.assertEqual("new-pdf", existing_pdf.read_text(encoding="utf-8"))
            self.assertEqual("existing", existing_txt.read_text(encoding="utf-8"))
            self.assertTrue(all(path.is_file() for path in expected))
            moved_audit = [
                entry for entry in organizer.last_audit
                if entry.get("action") == "moved_with_conflict_name"
            ]
            self.assertEqual(4, len(moved_audit))
            conflict_audit = next(
                entry for entry in organizer.last_audit
                if entry.get("action") == "name_conflict"
            )
            self.assertEqual(conflict_stem, conflict_audit["conflict_stem"])
            self.assertEqual(4, conflict_audit["affected_artifacts"])

    def test_reorganizing_published_package_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            final_dir = Path(tmpdir) / "final"
            staging = final_dir / "_staging" / "job"
            staging.mkdir(parents=True)
            artifacts = {
                "pdf": staging / "document.pdf",
                "quality": staging / "document_quality_report.json",
            }
            for role, path in artifacts.items():
                path.write_text(role, encoding="utf-8")
            organizer = DocumentOrganizer(final_dir)
            first = organizer.organize_artifacts(artifacts, "Jan/Archiv", package_id="job")

            second = organizer.organize_artifacts(
                {"pdf": first[0], "quality": first[1]},
                "Jan/Archiv",
                package_id="job",
            )

            self.assertEqual(first, second)
            actions = [
                entry["action"] for entry in organizer.last_audit
                if entry.get("artifact_role")
            ]
            self.assertEqual(["already_in_place", "already_in_place"], actions)
            self.assertFalse(any("_conflict_" in path.name for path in second))

    def test_identical_reingest_uses_existing_package_without_conflict_split(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            final_dir = Path(tmpdir) / "final"
            first_staging = final_dir / "_staging" / "job-1"
            second_staging = final_dir / "_staging" / "job-2"
            first_staging.mkdir(parents=True)
            second_staging.mkdir(parents=True)
            names = ("document.pdf", "document_quality_report.json")
            for directory in (first_staging, second_staging):
                (directory / names[0]).write_text("same-pdf", encoding="utf-8")
                (directory / names[1]).write_text("same-quality", encoding="utf-8")

            organizer = DocumentOrganizer(final_dir)
            first = organizer.organize_artifacts(
                [first_staging / name for name in names],
                "Jan/Archiv",
                package_id="job-1",
            )
            second = organizer.organize_artifacts(
                [second_staging / name for name in names],
                "Jan/Archiv",
                package_id="job-2",
            )

            self.assertEqual(first, second)
            actions = [
                entry["action"] for entry in organizer.last_audit
                if entry.get("artifact_role") is not None
            ]
            self.assertEqual(
                ["duplicate_kept_existing", "duplicate_kept_existing"],
                actions,
            )
            self.assertFalse(any(
                entry.get("action") == "name_conflict"
                for entry in organizer.last_audit
            ))


class TestRegistryDurability(unittest.TestCase):
    def test_registry_write_error_is_propagated_and_memory_is_not_partially_mutated(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            registry = FolderRegistry(root)
            registry.add_person("Jan")

            with patch("core.cloud.folder_registry.os.replace", side_effect=OSError("disk full")):
                with self.assertRaises(RegistryWriteError):
                    registry.add_path("Jan/Auto")

            self.assertNotIn("Jan/Auto", registry.get_known_paths())
            self.assertNotIn("Jan/Auto", FolderRegistry(root).get_known_paths())

    def test_tree_and_contexts_are_committed_together(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            registry = FolderRegistry(root)
            tree = {"Jan": {"Auto": {}, "Finanzen": {}}}

            registry.save_tree(
                tree,
                path_contexts={
                    "Jan/Auto": {"keywords": ["AB CD 123"], "notes": "Fahrzeugakte"},
                    "Nicht/Im/Baum": {"keywords": ["ignorieren"]},
                },
            )

            reloaded = FolderRegistry(root)
            self.assertEqual(["AB CD 123"], reloaded.get_path_context("Jan/Auto")["keywords"])
            self.assertNotIn("Nicht/Im/Baum", reloaded.get_path_contexts())


class TestPersistentReviewRecovery(unittest.TestCase):
    def test_review_and_staging_payload_survive_restart_and_can_resume(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / "input.pdf"
            source.write_text("pdf", encoding="utf-8")
            store = LocalStore(root)
            store.start_job("job-1", source, "abc", payload={"phase": "ocr"})
            store.update_job(
                "job-1",
                "deferred",
                artifacts={"pdf": "final/_staging/job-1/input.pdf"},
                quality={"score": 0.61, "review_required": True},
            )
            item_id = store.add_staging_item(
                job_id="job-1",
                source_name="input.pdf",
                proposed_path="Jan/Auto",
                artifacts={"pdf": "final/_staging/job-1/input.pdf"},
                payload={"fused_text": "Inhalt", "metadata": {"document_type": "Rechnung"}},
                quality={"score": 0.61, "review_required": True},
                candidates=[{"path": "Jan/Auto", "score": 61}],
            )

            recovered_store = LocalStore(root)
            recovered = recovered_store.get_review_item(item_id)
            self.assertEqual("staged", recovered["status"])
            self.assertEqual("final/_staging/job-1/input.pdf", recovered["artifacts"]["pdf"])
            self.assertEqual("Inhalt", recovered["payload"]["fused_text"])
            self.assertTrue(recovered["quality"]["review_required"])
            self.assertEqual("staged", recovered_store.get_job("job-1")["status"])

            opened = recovered_store.open_review_item(item_id)
            self.assertEqual("in_review", opened["status"])
            updated = recovered_store.update_review_item(
                item_id,
                metadata={"document_type": "Werkstattrechnung"},
            )
            self.assertEqual("Werkstattrechnung", updated["metadata"]["document_type"])
            resolved = recovered_store.resolve_review_item(item_id, "Jan/Auto/Rechnungen")
            self.assertEqual("resolved", resolved["status"])
            self.assertEqual("ready_to_resume", recovered_store.get_job("job-1")["status"])
            resumed = recovered_store.resume_review_item(item_id)
            self.assertEqual("pending", resumed["status"])
            self.assertEqual(1, resumed["resume_count"])
            self.assertEqual(item_id, recovered_store.list_recoverable_work()[0]["id"])

    def test_unversioned_legacy_database_is_migrated_in_place(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            db_path = root / "unified_ocr.sqlite3"
            with closing(sqlite3.connect(db_path)) as conn:
                with conn:
                    conn.execute("""
                    CREATE TABLE jobs (
                        job_id TEXT PRIMARY KEY, status TEXT NOT NULL,
                        source_name TEXT, source_path TEXT, source_sha256 TEXT,
                        final_name TEXT, target_path TEXT, metadata_json TEXT,
                        error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                    )
                """)
                    conn.execute("""
                    CREATE TABLE job_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT,
                        event TEXT NOT NULL, stage TEXT, status TEXT,
                        payload_json TEXT, created_at TEXT NOT NULL
                    )
                """)
                    conn.execute("""
                    CREATE TABLE documents (
                        id INTEGER PRIMARY KEY AUTOINCREMENT, source_sha256 TEXT,
                        source_name TEXT, final_name TEXT, target_path TEXT,
                        outputs_json TEXT, metadata_json TEXT,
                        created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                    )
                """)
                    conn.execute("""
                    CREATE TABLE review_queue (
                        id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT,
                        kind TEXT NOT NULL, status TEXT NOT NULL, source_name TEXT,
                        proposed_path TEXT, chosen_path TEXT, candidates_json TEXT,
                        metadata_json TEXT, created_at TEXT NOT NULL, resolved_at TEXT
                    )
                """)
                    conn.execute("""
                    INSERT INTO review_queue (
                        job_id, kind, status, source_name, proposed_path,
                        candidates_json, metadata_json, created_at
                    ) VALUES ('legacy-job', 'new_path', 'pending', 'old.pdf',
                              'Jan/Alt', '[]', '{}', '2026-01-01T00:00:00+00:00')
                """)

            store = LocalStore(root)
            legacy = store.list_review_items("pending")[0]
            self.assertEqual({}, legacy["payload"])
            self.assertEqual({}, legacy["artifacts"])
            self.assertEqual({}, legacy["quality"])
            with closing(sqlite3.connect(db_path)) as conn:
                version = conn.execute("PRAGMA user_version").fetchone()[0]
            self.assertEqual(SCHEMA_VERSION, version)


if __name__ == "__main__":
    unittest.main()
