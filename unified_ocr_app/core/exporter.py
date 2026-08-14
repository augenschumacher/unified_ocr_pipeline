"""Output generation for processed OCR jobs."""

from __future__ import annotations

import json
import os
import re
import shutil
import uuid
from pathlib import Path

from core.cache import sha256_file
from core.docx_tools import save_markdown_as_docx
from core.ocr import inject_fused_text_and_metadata, validate_archival_pdf


def _atomic_write_text(path: Path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
            temporary.unlink(missing_ok=True)


def write_quality_report_atomic(path: Path, report: dict) -> None:
    """Durably replace a quality sidecar with canonical UTF-8 JSON."""
    _atomic_write_text(
        Path(path),
        json.dumps(report if isinstance(report, dict) else {}, indent=4, ensure_ascii=False, default=str),
    )


def _publish_without_overwrite(staged: Path, destination: Path) -> None:
    """Publish one staged file atomically while refusing any existing name."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(staged, destination)
        staged.unlink()
        return
    except FileExistsError:
        raise
    except OSError:
        descriptor = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            reservation = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        try:
            current = destination.stat()
            if (current.st_dev, current.st_ino, current.st_size) != (
                reservation.st_dev,
                reservation.st_ino,
                0,
            ):
                raise FileExistsError(f"Zieldatei wurde zwischenzeitlich verändert: {destination}")
            os.replace(staged, destination)
        except Exception:
            try:
                if destination.is_file() and destination.stat().st_size == 0:
                    destination.unlink()
            except OSError:
                pass
            raise


class DocumentExporter:
    def __init__(
        self,
        *,
        config,
        output_format: str,
        docx_mode: str,
        save_docx_enabled: bool,
        save_json_enabled: bool,
        gdrive_enabled: bool,
        gdrive_upload_docx: bool,
        gdrive_upload_json: bool,
        log_callback,
        save_docx_func=save_markdown_as_docx,
        inject_pdf_func=inject_fused_text_and_metadata,
        validate_archival_pdf_func=validate_archival_pdf,
        validate_archival_pdf_enabled: bool = False,
    ):
        self.config = config
        self.output_format = output_format
        self.docx_mode = docx_mode
        self.save_docx_enabled = save_docx_enabled
        self.save_json_enabled = save_json_enabled
        self.gdrive_enabled = gdrive_enabled
        self.gdrive_upload_docx = gdrive_upload_docx
        self.gdrive_upload_json = gdrive_upload_json
        self.log = log_callback
        self.save_docx_func = save_docx_func
        self.inject_pdf_func = inject_pdf_func
        self.validate_archival_pdf_func = validate_archival_pdf_func
        self.validate_archival_pdf_enabled = bool(validate_archival_pdf_enabled)
        self.last_final_name = ""
        self._recover_interrupted_exports()

    @staticmethod
    def _process_is_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        if pid == os.getpid():
            return True
        if os.name == "nt":
            try:
                import ctypes

                process_query_limited_information = 0x1000
                still_active = 259
                kernel32 = ctypes.windll.kernel32
                handle = kernel32.OpenProcess(
                    process_query_limited_information,
                    False,
                    int(pid),
                )
                if not handle:
                    # Access denied means the process exists but cannot be
                    # queried; an unknown/missing PID is safe to recover.
                    return int(kernel32.GetLastError()) == 5
                try:
                    exit_code = ctypes.c_ulong()
                    if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                        return True
                    return int(exit_code.value) == still_active
                finally:
                    kernel32.CloseHandle(handle)
            except Exception:
                return True
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True

    @staticmethod
    def _write_export_journal(path: Path, payload: dict) -> None:
        _atomic_write_text(path, json.dumps(payload, indent=2, ensure_ascii=False))

    def _prepare_export_journal(
        self,
        transaction_dir: Path,
        reservation: Path,
        generated: list[tuple[str, Path, Path]],
    ) -> Path:
        journal_path = transaction_dir / "export_transaction.json"
        payload = {
            "schema": "unified_ocr_export_transaction_v1",
            "transaction_id": transaction_dir.name,
            "pid": os.getpid(),
            "state": "prepared",
            "reservation": str(reservation),
            "committed_roles": [],
            "members": [
                {
                    "role": role,
                    "staged": str(staged),
                    "destination": str(destination),
                    "sha256": sha256_file(staged),
                    "size_bytes": staged.stat().st_size,
                }
                for role, staged, destination in generated
            ],
        }
        self._write_export_journal(journal_path, payload)
        return journal_path

    def _recover_interrupted_exports(self) -> None:
        """Complete crash-interrupted package commits from their hash journal."""
        root = Path(self.config.final_dir)
        transaction_root = root / "_export_transactions"
        if not transaction_root.is_dir():
            return
        root_resolved = root.resolve(strict=False)
        reservation_root = (root / "_export_reservations").resolve(strict=False)
        for transaction_dir in sorted(transaction_root.iterdir()):
            if not transaction_dir.is_dir():
                continue
            journal_path = transaction_dir / "export_transaction.json"
            if not journal_path.is_file():
                continue
            try:
                payload = json.loads(journal_path.read_text(encoding="utf-8"))
                if payload.get("schema") != "unified_ocr_export_transaction_v1":
                    continue
                if self._process_is_alive(int(payload.get("pid") or 0)):
                    continue
                members = payload.get("members")
                if not isinstance(members, list) or not members:
                    continue
                committed_roles = []
                for member in members:
                    if not isinstance(member, dict):
                        raise RuntimeError("Ungültiger Member im Exportjournal.")
                    role = str(member.get("role") or "")
                    expected_hash = str(member.get("sha256") or "")
                    staged = Path(str(member.get("staged") or ""))
                    destination = Path(str(member.get("destination") or ""))
                    if not role or not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
                        raise RuntimeError("Ungültiger Hash/Rolle im Exportjournal.")
                    staged.resolve(strict=False).relative_to(transaction_dir.resolve(strict=False))
                    destination.resolve(strict=False).relative_to(root_resolved)
                    if destination.is_file():
                        if sha256_file(destination) != expected_hash:
                            raise FileExistsError(
                                f"Export-Recovery blockiert: Zielinhalt weicht ab: {destination}"
                            )
                        if staged.is_file() and sha256_file(staged) == expected_hash:
                            staged.unlink()
                    else:
                        if not staged.is_file() or sha256_file(staged) != expected_hash:
                            raise FileNotFoundError(
                                f"Export-Recovery findet weder gültiges Staging noch Ziel: {role}"
                            )
                        _publish_without_overwrite(staged, destination)
                    committed_roles.append(role)
                    payload["state"] = "committing"
                    payload["committed_roles"] = committed_roles
                    self._write_export_journal(journal_path, payload)

                payload["state"] = "committed"
                self._write_export_journal(journal_path, payload)
                reservation = Path(str(payload.get("reservation") or ""))
                try:
                    reservation.resolve(strict=False).relative_to(reservation_root)
                except ValueError:
                    reservation = Path()
                shutil.rmtree(transaction_dir, ignore_errors=True)
                if reservation and reservation.is_file():
                    self._release_reservation(reservation)
                self.log(
                    f"Unterbrochenes Exportpaket '{transaction_dir.name}' wurde vollständig wiederhergestellt."
                )
            except Exception as exc:
                self.log(
                    f"Export-Recovery für '{transaction_dir.name}' blockiert: {exc}. "
                    "Journal und Dateien bleiben zur manuellen Prüfung erhalten."
                )

    def _conflict_free_package_name(self, requested_name: str) -> str:
        """Choose one unused stem for every derivative in the package."""
        root = Path(self.config.final_dir)
        companion = root / "begleitdateien"

        def occupied(stem: str) -> bool:
            candidates = (
                root / f"{stem}.pdf",
                root / f"{stem}.txt",
                root / f"{stem}.docx",
                companion / f"{stem}.docx",
                companion / f"{stem}_quality_report.json",
            )
            return any(path.exists() for path in candidates)

        if not occupied(requested_name):
            return requested_name
        for index in range(1, 100_000):
            candidate = f"{requested_name}_conflict_{index:03d}"
            if not occupied(candidate):
                return candidate
        raise FileExistsError(f"Kein konfliktfreier Paketname für {requested_name!r} verfügbar.")

    def _reserve_package_name(self, requested_name: str) -> tuple[str, Path]:
        root = Path(self.config.final_dir)
        reservation_dir = root / "_export_reservations"
        reservation_dir.mkdir(parents=True, exist_ok=True)
        for index in range(0, 100_000):
            candidate = requested_name if index == 0 else f"{requested_name}_conflict_{index:03d}"
            lock_path = reservation_dir / f"{candidate}.lock"
            try:
                descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                continue
            try:
                os.write(descriptor, f"pid={os.getpid()}\n".encode("ascii"))
            finally:
                os.close(descriptor)
            if self._conflict_free_package_name(candidate) == candidate:
                return candidate, lock_path
            lock_path.unlink(missing_ok=True)
        raise FileExistsError(f"Kein exklusiv reservierbarer Paketname für {requested_name!r} verfügbar.")

    @staticmethod
    def _release_reservation(lock_path: Path) -> None:
        lock_path.unlink(missing_ok=True)
        try:
            lock_path.parent.rmdir()
        except OSError:
            pass

    def _commit_package(
        self,
        generated: list[tuple[str, Path, Path]],
        journal_path: Path,
    ) -> dict:
        committed: list[tuple[Path, Path]] = []
        payload = json.loads(journal_path.read_text(encoding="utf-8"))
        committed_roles: list[str] = []
        try:
            for role, staged, destination in generated:
                _publish_without_overwrite(staged, destination)
                committed.append((destination, staged))
                committed_roles.append(role)
                payload["state"] = "committing"
                payload["committed_roles"] = list(committed_roles)
                self._write_export_journal(journal_path, payload)
        except Exception:
            for destination, staged in reversed(committed):
                try:
                    if destination.exists() and not staged.exists():
                        os.replace(destination, staged)
                except OSError:
                    pass
            raise
        payload["state"] = "committed"
        self._write_export_journal(journal_path, payload)
        return {role: destination for role, _staged, destination in generated}

    def export(
        self,
        work_pdf: Path,
        fused_pages: dict,
        fused_text: str,
        final_name: str,
        metadata: dict,
        image_paths: list,
        quality_report: dict,
        *,
        is_docx: bool = False,
    ) -> dict:
        """Generate configured derivatives and return their paths.

        For PDF output, work_pdf is the authoritative OCR/archival source.
        Its existing page objects and OCR text layer are retained. fused_pages
        remains an API-compatible input but is never treated as a geometrically
        valid OCR overlay; fused_text supplies the separate text derivatives.
        """
        final_name, reservation = self._reserve_package_name(final_name)
        self.last_final_name = final_name
        root = Path(self.config.final_dir)
        transaction_dir = root / "_export_transactions" / uuid.uuid4().hex
        generated: list[tuple[str, Path, Path]] = []
        created = {"pdf": None, "txt": None, "docx": None, "json": None}
        if not isinstance(quality_report, dict):
            quality_report = {}
        try:
            # Keep name-reservation cleanup inside the same failure boundary as
            # transaction-directory creation (permissions/full disk can fail
            # before the first derivative is generated).
            transaction_dir.mkdir(parents=True, exist_ok=False)
            fmt = self.output_format
            docx_text_input = (
                fused_pages
                if self.docx_mode == "Prüf-DOCX" and isinstance(fused_pages, dict) and fused_pages
                else fused_text
            )

            if is_docx:
                orig_suffix = work_pdf.suffix.lower()
                staged_docx = transaction_dir / f"{final_name}.docx"
                if orig_suffix == ".docx":
                    shutil.copy2(work_pdf, staged_docx)
                else:
                    self.log(f"Konvertiere {orig_suffix.upper()[1:]} zu DOCX...")
                    self.save_docx_func(
                        docx_text_input,
                        staged_docx,
                        mode=self.docx_mode,
                        image_paths=image_paths,
                        quality_report=quality_report,
                    )
                generated.append(("docx", staged_docx, root / staged_docx.name))
            elif fmt in ("Nur PDF", "PDF und TXT", "PDF und DOCX"):
                staged_pdf = transaction_dir / f"{final_name}.pdf"
                self.log("Finalisiere PDF verlustfrei (Quellseiten und OCR-Textlayer bleiben erhalten)...")
                self.inject_pdf_func(work_pdf, staged_pdf, fused_pages, metadata)
                if self.validate_archival_pdf_enabled:
                    postflight = self.validate_archival_pdf_func(staged_pdf)
                    if not isinstance(postflight, dict):
                        postflight = {
                            "ok": False,
                            "error": "Archiv-PDF-Postflight lieferte keinen strukturierten Bericht.",
                        }
                    # Persist the durable publication path, not the private
                    # transaction path that is removed immediately after the
                    # package commit.
                    postflight["path"] = str(root / staged_pdf.name)
                    quality_report["pdf_postflight"] = postflight
                    if not postflight.get("ok"):
                        raise RuntimeError(postflight.get("error") or "Archiv-PDF-Postflight fehlgeschlagen.")
                generated.append(("pdf", staged_pdf, root / staged_pdf.name))

            if fmt in ("Nur TXT", "PDF und TXT") or (is_docx and fmt == "PDF und TXT"):
                staged_txt = transaction_dir / f"{final_name}.txt"
                _atomic_write_text(staged_txt, fused_text)
                generated.append(("txt", staged_txt, root / staged_txt.name))

            if not is_docx:
                should_generate_docx = (
                    (fmt in ("Nur DOCX", "PDF und DOCX") and self.save_docx_enabled)
                    or (self.gdrive_enabled and self.gdrive_upload_docx)
                )
                if should_generate_docx:
                    staged_docx = transaction_dir / f"{final_name}.docx"
                    self.log(f"Erstelle DOCX (Modus: {self.docx_mode})...")
                    self.save_docx_func(
                        docx_text_input,
                        staged_docx,
                        mode=self.docx_mode,
                        image_paths=image_paths,
                        quality_report=quality_report,
                    )
                    generated.append(
                        ("docx", staged_docx, root / "begleitdateien" / staged_docx.name)
                    )
                else:
                    self.log("DOCX-Erstellung übersprungen (in den Einstellungen deaktiviert).")

            should_generate_json = self.save_json_enabled or (
                self.gdrive_enabled and self.gdrive_upload_json
            )
            if should_generate_json:
                staged_json = transaction_dir / f"{final_name}_quality_report.json"
                write_quality_report_atomic(staged_json, quality_report)
                generated.append(
                    ("json", staged_json, root / "begleitdateien" / staged_json.name)
                )

            if not generated:
                raise RuntimeError("Die Exportkonfiguration erzeugt kein Dokumentartefakt.")
            journal_path = self._prepare_export_journal(
                transaction_dir,
                reservation,
                generated,
            )
            committed = self._commit_package(generated, journal_path)
            created.update(committed)
            for role, destination in committed.items():
                if role == "json":
                    self.log(f"-> Qualitätsbericht: begleitdateien/{destination.name}")
                elif role == "docx" and destination.parent.name == "begleitdateien":
                    self.log(f"-> DOCX: begleitdateien/{destination.name}")
                else:
                    self.log(f"-> {role.upper()}: {destination.name}")
            return created
        finally:
            shutil.rmtree(transaction_dir, ignore_errors=True)
            try:
                transaction_dir.parent.rmdir()
            except OSError:
                pass
            self._release_reservation(reservation)
