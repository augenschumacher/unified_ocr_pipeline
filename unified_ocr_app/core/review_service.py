"""Application service for durable, human-approved archive review work."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from core.cache import sha256_file
from core.cloud.classification_memory import ClassificationMemory
from core.cloud.folder_registry import FolderRegistry, UnsafeArchivePath, normalize_archive_path
from core.cloud.organizer import DocumentOrganizer
from core.job_history import JobHistory
from core.local_store import LocalStore, RECOVERABLE_REVIEW_STATUSES
from core.metadata import normalize_metadata
from core.docx_tools import save_markdown_as_docx
from core.ocr import update_archival_pdf_metadata
from core.quality import QualityChecker


class ReviewResolutionError(RuntimeError):
    """Raised when review work cannot be safely published."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_json_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.review.tmp")
    try:
        temporary.write_text(
            json.dumps(data, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_text_write(path: Path, text: str) -> None:
    temporary = path.with_name(f".{path.name}.review.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class ReviewQueueService:
    """Resolve a staged document package as one auditable local operation.

    OCR-quality items require an explicit quality confirmation.  Folder choice
    alone is deliberately insufficient.  Files are always moved by the
    rollback-capable package organizer before the review is marked resolved or
    the classifier learning store is updated.
    """

    def __init__(self, config):
        self.config = config
        self.store = LocalStore(config)
        self.registry = FolderRegistry(config.base_dir)
        self.organizer = DocumentOrganizer(config.final_dir)

    def list_open(self, limit: int = 200) -> list[dict]:
        return self.store.list_recoverable_work(limit=limit)

    def known_paths(self) -> list[str]:
        paths = self.registry.get_known_paths()
        return paths or ["Sonstiges"]

    def preview_path(self, item: dict) -> Path | None:
        for _role, path in self._artifact_items(item):
            if path.suffix.casefold() == ".pdf" and path.is_file():
                return path
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        candidate = Path(str(payload.get("preview_pdf_path") or ""))
        return candidate if candidate.is_file() else None

    @staticmethod
    def original_path(item: dict) -> Path | None:
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        candidate = Path(str(payload.get("original_path") or ""))
        return candidate if candidate.is_file() else None

    def review_readiness(self, item: dict) -> tuple[bool, str]:
        """Check whether a queue row can be resolved without claiming it.

        This is intentionally read-only so the UI can explain stale imported
        rows before offering an action that is guaranteed to fail.
        """

        try:
            artifacts = self._artifact_items(item, require_all=True)
        except ReviewResolutionError as exc:
            return False, str(exc)
        if not artifacts:
            return False, "Das Review-Paket enthält keine wiederherstellbaren Dateien."

        if self._review_requires_manifest(item):
            manifest_path = next(
                (path for _role, path in artifacts if path.name.endswith("_job_manifest.json")),
                None,
            )
            if manifest_path is None:
                return False, "Das Archivmanifest des Review-Pakets fehlt."
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except Exception as exc:
                return False, f"Das Archivmanifest ist nicht lesbar: {exc}"
            if not isinstance(manifest, dict) or str(manifest.get("job_id") or "") != str(
                item.get("job_id") or ""
            ):
                return False, "Das Archivmanifest gehört nicht eindeutig zu diesem Review-Auftrag."
        return True, ""

    def resolve(
        self,
        item_id: int,
        chosen_path: str,
        *,
        quality_confirmed: bool = False,
        review_note: str = "",
        corrected_text: str | None = None,
        corrected_metadata: dict | None = None,
        post_publish_callback: Callable[[dict], list[dict] | dict | None] | None = None,
    ) -> dict:
        item = self.store.get_review_item(item_id)
        if not item:
            raise ReviewResolutionError(f"Review-Eintrag #{item_id} existiert nicht.")
        if item.get("status") not in RECOVERABLE_REVIEW_STATUSES:
            raise ReviewResolutionError("Der Review-Eintrag ist nicht mehr offen.")
        if item.get("kind") == "ocr_quality" and not quality_confirmed:
            raise ReviewResolutionError(
                "Die OCR-Qualität muss vor der Ablage ausdrücklich bestätigt werden."
            )

        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        publish_to_root = payload.get("organize_enabled") is False
        target_path = "" if publish_to_root else self._validate_target(chosen_path)
        stored_target_path = "__archive_root__" if publish_to_root else target_path

        claimed = self.store.claim_review_item(
            item_id,
            expected_revision=int(item.get("revision") or 0),
        )
        if not claimed:
            raise ReviewResolutionError(
                "Dieser Review-Eintrag wird bereits verarbeitet oder wurde zwischenzeitlich geändert."
            )
        item = claimed
        claim_token = str(item.get("claim_token") or "")
        if not claim_token:
            raise ReviewResolutionError("Der Review-Claim besitzt kein Eigentümer-Token.")
        require_manifest = self._review_requires_manifest(item)
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        artifacts = self._claimed_artifact_items(
            item,
            item_id=item_id,
            claim_token=claim_token,
            payload=payload,
            require_manifest=require_manifest,
        )
        if not artifacts:
            raise ReviewResolutionError("Das Review-Paket enthält keine wiederherstellbaren Dateien.")

        effective_metadata = dict(item.get("metadata") or {})
        effective_payload = dict(payload)
        effective_quality = dict(item.get("quality") or {})
        review_record: dict[str, Any] = {}
        try:
            (
                effective_metadata,
                effective_payload,
                text_changed,
                metadata_changed,
                machine_text,
                effective_text,
            ) = self._prepare_human_corrections(
                item,
                corrected_text=corrected_text,
                corrected_metadata=corrected_metadata,
            )
            quality = item.get("quality") if isinstance(item.get("quality"), dict) else {}
            review_record = {
                "status": "confirmed",
                "review_item_id": item_id,
                "kind": item.get("kind"),
                "chosen_path": stored_target_path,
                "quality_confirmed": bool(quality_confirmed),
                "note": str(review_note or "").strip(),
                "text_changed": text_changed,
                "metadata_changed": metadata_changed,
                "confirmed_at": _now_iso(),
            }
            effective_quality = dict(quality)
            if text_changed:
                post_edit_validation = QualityChecker.run_quality_check(
                    machine_text,
                    "",
                    "",
                    effective_text,
                )
                effective_quality["post_edit_validation"] = post_edit_validation
                review_record["post_edit_quality_status"] = post_edit_validation.get(
                    "quality_status"
                )
                review_record["post_edit_quality_score"] = post_edit_validation.get(
                    "quality_score"
                )
            effective_quality["human_review"] = review_record
            effective_quality["reviewed_metadata"] = effective_metadata
            effective_payload["pending_resolution"] = review_record

            # Persist the authoritative human values before touching derivative
            # files.  If the process stops, the queue reopens with the reviewed
            # values and pending flags cause the idempotent writes to be retried.
            self.store.update_review_item(
                item_id,
                status="in_review",
                claim_token=claim_token,
                artifacts={role: str(path) for role, path in artifacts},
                metadata=effective_metadata,
                payload=effective_payload,
                quality=effective_quality,
                error="",
            )
            artifacts = self._persist_human_corrections(
                item,
                artifacts,
                effective_text=effective_text,
                text_changed=text_changed,
                quality=effective_quality,
                payload=effective_payload,
            )
            if metadata_changed:
                self._synchronize_reviewed_pdf_metadata(artifacts, effective_metadata)
            self._update_quality_sidecar(
                artifacts,
                effective_quality,
                effective_metadata,
                review_record,
            )
            effective_payload["text_correction_pending"] = False
            effective_payload["metadata_correction_pending"] = False
            self.store.update_review_item(
                item_id,
                status="in_review",
                claim_token=claim_token,
                artifacts={role: str(path) for role, path in artifacts},
                metadata=effective_metadata,
                payload=effective_payload,
                quality=effective_quality,
                error="",
            )
        except Exception as exc:
            try:
                self.store.update_review_item(
                    item_id,
                    status="failed",
                    claim_token=claim_token,
                    artifacts={role: str(path) for role, path in artifacts if path.exists()},
                    metadata=effective_metadata,
                    payload=effective_payload,
                    quality=effective_quality,
                    error=str(exc),
                )
            except Exception:
                pass
            if isinstance(exc, PermissionError) or getattr(exc, "winerror", None) in {5, 32}:
                message = (
                    "Die PDF ist noch in einer Vorschau oder einem anderen PDF-Programm geöffnet. "
                    "Schließen Sie externe PDF-Fenster und klicken Sie anschließend erneut auf "
                    "den grünen Bestätigungsbutton. Die Prüfung bleibt vollständig erhalten."
                )
            else:
                message = f"Korrekturen konnten nicht gespeichert werden: {exc}"
            raise ReviewResolutionError(message) from exc

        mapping = {role: path for role, path in artifacts}
        moved_paths: list[Path] = []
        target_dir = (
            Path(self.config.final_dir)
            if publish_to_root
            else Path(self.config.final_dir).joinpath(*target_path.split("/"))
        )
        move_intent = self._build_move_intent(
            artifacts,
            target_dir=target_dir,
            target_label=stored_target_path,
        )
        effective_payload["move_intent"] = move_intent
        try:
            self.store.update_review_item(
                item_id,
                status="in_review",
                claim_token=claim_token,
                artifacts={role: str(path) for role, path in artifacts},
                metadata=effective_metadata,
                payload=effective_payload,
                quality=effective_quality,
                error="",
            )
            if publish_to_root:
                moved_paths = self.organizer.move_artifacts_to_directory(
                    mapping,
                    self.config.final_dir,
                    target_label="archive_root",
                    package_id=item.get("job_id") or f"review-{item_id}",
                )
            else:
                moved_paths = self.organizer.organize_artifacts(
                    mapping,
                    target_path,
                    package_id=item.get("job_id") or f"review-{item_id}",
                )
            if len(moved_paths) != len(mapping):
                raise ReviewResolutionError("Das Dokumentpaket wurde nicht vollständig verschoben.")

            # Register only after a complete package commit.  If registry
            # persistence fails, the item remains recoverable and points at
            # the committed files; no file is silently lost.
            if not publish_to_root and target_path not in self.registry.get_known_paths():
                added = self.registry.add_path(target_path)
                if not added and target_path not in FolderRegistry(self.config.base_dir).get_known_paths():
                    raise ReviewResolutionError(
                        f"Der Zielpfad '{target_path}' konnte nicht registriert werden."
                    )

            moved_artifacts = {
                role: str(path)
                for (role, _source), path in zip(artifacts, moved_paths)
            }
            effective_payload["move_intent"] = {
                **move_intent,
                "phase": "committed",
                "destinations": moved_artifacts,
                "committed_at": _now_iso(),
            }
            # Close the filesystem/SQLite crash window immediately after the
            # package commit.  Later manifest/index work can safely be retried.
            self.store.update_review_item(
                item_id,
                status="in_review",
                claim_token=claim_token,
                artifacts=moved_artifacts,
                metadata=effective_metadata,
                payload=effective_payload,
                quality=effective_quality,
                error="",
            )
            # The durable paths are now persisted, so rewriting the quality
            # sidecar cannot break move recovery: a crash can reopen the item
            # directly from ``artifacts`` without relying on pre-move hashes.
            pdf_path = next(
                (
                    Path(path)
                    for role, path in moved_artifacts.items()
                    if role == "pdf" or Path(path).suffix.casefold() == ".pdf"
                ),
                None,
            )
            postflight = effective_quality.get("pdf_postflight")
            if pdf_path and pdf_path.is_file() and isinstance(postflight, dict):
                postflight["path"] = str(pdf_path)
                self._update_quality_sidecar(
                    list(zip(moved_artifacts, moved_paths)),
                    effective_quality,
                    effective_metadata,
                    review_record,
                )
                self.store.update_review_item(
                    item_id,
                    status="in_review",
                    claim_token=claim_token,
                    artifacts=moved_artifacts,
                    metadata=effective_metadata,
                    payload=effective_payload,
                    quality=effective_quality,
                    error="",
                )
            final_payload = {
                **effective_payload,
                "resolution": review_record,
                "organization_audit": list(self.organizer.last_audit),
            }
            sync_audit: list[dict] = []
            if post_publish_callback is not None:
                final_payload["sync_intent"] = {
                    "phase": "prepared",
                    "target_path": target_path,
                    "prepared_at": _now_iso(),
                }
                effective_payload = final_payload
                self.store.update_review_item(
                    item_id,
                    status="in_review",
                    claim_token=claim_token,
                    artifacts=moved_artifacts,
                    metadata=effective_metadata,
                    payload=final_payload,
                    quality=effective_quality,
                    error="",
                )
                # Validate and persist the local manifest before any remote
                # side effect.  A malformed manifest must never be followed by
                # uploads that the local audit trail cannot account for.
                self._update_manifest(
                    moved_paths,
                    moved_artifacts,
                    review_record,
                    effective_metadata,
                    effective_quality,
                    status="review_sync_pending",
                    sync_phase="prepared",
                    sync_audit=[],
                    require_manifest=require_manifest,
                )
                callback_context = {
                    "target_path": target_path,
                    "artifacts": moved_artifacts,
                    "item": {
                        **item,
                        "artifacts": moved_artifacts,
                        "metadata": effective_metadata,
                        "payload": final_payload,
                        "quality": effective_quality,
                    },
                    "heartbeat": lambda: self.store.refresh_review_claim(
                        item_id,
                        claim_token,
                    ),
                }
                try:
                    sync_audit = self._normalise_sync_audit(
                        post_publish_callback(callback_context)
                    )
                except Exception as exc:
                    sync_audit = [{"action": "failed", "error": str(exc)}]
                sync_failed = any(
                    str(entry.get("action") or "").casefold() == "failed"
                    for entry in sync_audit
                )
                final_payload["sync_audit"] = sync_audit
                final_payload["sync_retry_required"] = sync_failed
                final_payload["sync_intent"] = {
                    **final_payload["sync_intent"],
                    "phase": "failed" if sync_failed else "committed",
                    "finished_at": _now_iso(),
                }
                effective_payload = final_payload
                self.store.update_review_item(
                    item_id,
                    status="in_review",
                    claim_token=claim_token,
                    artifacts=moved_artifacts,
                    metadata=effective_metadata,
                    payload=final_payload,
                    quality=effective_quality,
                    error="",
                )
                self._update_manifest(
                    moved_paths,
                    moved_artifacts,
                    review_record,
                    effective_metadata,
                    effective_quality,
                    status="sync_failed" if sync_failed else "completed_after_review",
                    sync_phase=final_payload["sync_intent"]["phase"],
                    sync_audit=sync_audit,
                    require_manifest=require_manifest,
                )
                if sync_failed:
                    raise ReviewResolutionError(
                        "Das Paket wurde lokal veröffentlicht, aber die Remote-Synchronisierung "
                        "ist fehlgeschlagen. Der Review-Eintrag bleibt wiederaufnehmbar."
                    )
            else:
                self._update_manifest(
                    moved_paths,
                    moved_artifacts,
                    review_record,
                    effective_metadata,
                    effective_quality,
                    status="completed_after_review",
                    sync_phase="not_configured",
                    sync_audit=[],
                    require_manifest=require_manifest,
                )

            resolved = self.store.finalize_review_transaction(
                item_id,
                claim_token=claim_token,
                chosen_path=stored_target_path,
                target_path=target_path,
                artifacts=moved_artifacts,
                metadata=effective_metadata,
                quality=effective_quality,
                payload=final_payload,
            )
            effective_item = {**item, "payload": effective_payload, "metadata": effective_metadata}
            post_finalize_warnings = self._finish_external_audit(
                item_id,
                effective_item,
                target_path,
                moved_artifacts,
                effective_metadata,
                effective_quality,
            )
            if not publish_to_root:
                try:
                    self._learn_confirmed_decision(
                        effective_item,
                        target_path,
                        effective_metadata,
                        decision_id=f"review-{item_id}",
                    )
                except Exception as exc:
                    post_finalize_warnings.append(f"Lernspeicher: {exc}")
            self._remove_empty_staging_parents(artifacts)
            return {
                "item": resolved,
                "target_path": target_path,
                "artifacts": moved_artifacts,
                "audit": list(self.organizer.last_audit),
                "sync_audit": sync_audit,
                "sync_completed": post_publish_callback is not None,
                "post_finalize_warnings": post_finalize_warnings,
            }
        except Exception as exc:
            if moved_paths:
                recoverable_artifacts = {
                    role: str(path)
                    for (role, _source), path in zip(artifacts, moved_paths)
                    if path.exists()
                }
            else:
                recoverable_artifacts = {
                    role: str(path) for role, path in artifacts if path.exists()
                }
            try:
                self.store.update_review_item(
                    item_id,
                    status="failed",
                    claim_token=claim_token,
                    artifacts=recoverable_artifacts,
                    metadata=effective_metadata,
                    quality=effective_quality,
                    error=str(exc),
                    payload={
                        **effective_payload,
                        "organization_audit": list(self.organizer.last_audit),
                    },
                )
            except Exception:
                pass
            if isinstance(exc, ReviewResolutionError):
                raise
            raise ReviewResolutionError(str(exc)) from exc

    def _validate_target(self, value: str) -> str:
        try:
            target = normalize_archive_path(value, max_depth=8)
        except UnsafeArchivePath as exc:
            raise ReviewResolutionError(str(exc)) from exc
        if target in self.registry.get_known_paths():
            return target
        top_level = target.split("/", 1)[0]
        person = next(
            (candidate for candidate in self.registry.get_persons() if candidate.casefold() == top_level.casefold()),
            None,
        )
        if not person:
            raise ReviewResolutionError(
                f"Der Hauptordner '{top_level}' ist nicht als Person/Aktenbestand registriert."
            )
        return "/".join([person, *target.split("/")[1:]])

    def _prepare_human_corrections(
        self,
        item: dict,
        *,
        corrected_text: str | None,
        corrected_metadata: dict | None,
    ) -> tuple[dict, dict, bool, bool, str, str]:
        payload = dict(item.get("payload") if isinstance(item.get("payload"), dict) else {})
        current_text = str(payload.get("fused_text") or "")
        machine_text = str(payload.get("machine_fused_text") or current_text)
        effective_text = current_text if corrected_text is None else str(corrected_text)
        if (
            corrected_text is not None
            and any(character.isalnum() for character in current_text)
            and not any(character.isalnum() for character in effective_text)
        ):
            raise ValueError(
                "Der geprüfte Volltext darf nicht versehentlich vollständig geleert werden."
            )
        text_changed = bool(payload.get("text_correction_pending")) or (
            corrected_text is not None and effective_text != current_text
        )
        original_metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        effective_metadata = (
            normalize_metadata(corrected_metadata, source_text=effective_text)
            if isinstance(corrected_metadata, dict)
            else dict(original_metadata)
        )
        metadata_changed = bool(payload.get("metadata_correction_pending")) or (
            effective_metadata != original_metadata
        )
        if text_changed and "machine_fused_text" not in payload:
            payload["machine_fused_text"] = current_text
        payload["fused_text"] = effective_text
        payload["text_correction_pending"] = text_changed
        payload["metadata_correction_pending"] = metadata_changed
        return (
            effective_metadata,
            payload,
            text_changed,
            metadata_changed,
            machine_text,
            effective_text,
        )

    def _persist_human_corrections(
        self,
        item: dict,
        artifacts: list[tuple[str, Path]],
        *,
        effective_text: str,
        text_changed: bool,
        quality: dict,
        payload: dict,
    ) -> list[tuple[str, Path]]:
        mutable_artifacts = list(artifacts)
        if not text_changed:
            return mutable_artifacts

        txt_artifacts = [path for _role, path in mutable_artifacts if path.suffix.lower() == ".txt"]
        if not txt_artifacts:
            parent = mutable_artifacts[0][1].parent
            safe_stem = re.sub(
                r"[^\w.-]+",
                "_",
                Path(str(item.get("source_name") or mutable_artifacts[0][1].stem)).stem,
                flags=re.UNICODE,
            ).strip("._") or "reviewed_document"
            reviewed_txt = parent / f"{safe_stem}_reviewed.txt"
            mutable_artifacts.append(("reviewed_txt", reviewed_txt))
            txt_artifacts.append(reviewed_txt)
        for path in txt_artifacts:
            _atomic_text_write(path, effective_text)

        # Never overwrite a DOCX that may be the born-digital original or an
        # image-bearing proof derivative.  For known generated derivatives we
        # add a clearly named, readable reviewed copy and preserve the machine
        # version as evidence.
        docx_artifacts = [
            (role, path)
            for role, path in mutable_artifacts
            if path.suffix.lower() == ".docx"
        ]
        original_suffix = Path(str(payload.get("original_path") or "")).suffix.lower()
        generated_docx_known = (
            payload.get("is_docx") is False
            or (payload.get("is_docx") is True and original_suffix not in {"", ".docx"})
        )
        if docx_artifacts and generated_docx_known:
            reviewed_entry = next(
                ((role, path) for role, path in docx_artifacts if role == "reviewed_docx"),
                None,
            )
            if reviewed_entry:
                reviewed_docx = reviewed_entry[1]
            else:
                base_docx = docx_artifacts[0][1]
                reviewed_docx = base_docx.with_name(f"{base_docx.stem}_reviewed.docx")
                mutable_artifacts.append(("reviewed_docx", reviewed_docx))
            temporary = reviewed_docx.with_name(f".{reviewed_docx.name}.review.tmp.docx")
            try:
                save_markdown_as_docx(
                    effective_text,
                    temporary,
                    mode="Lesbare DOCX",
                    image_paths=[],
                    quality_report=quality,
                )
                os.replace(temporary, reviewed_docx)
            finally:
                temporary.unlink(missing_ok=True)
        return mutable_artifacts

    @staticmethod
    def _synchronize_reviewed_pdf_metadata(
        artifacts: list[tuple[str, Path]],
        metadata: dict,
    ) -> None:
        for role, path in artifacts:
            if path.suffix.lower() != ".pdf" or "original" in role.casefold():
                continue
            update_archival_pdf_metadata(path, metadata, require_backend=True)

    @staticmethod
    def _build_move_intent(
        artifacts: list[tuple[str, Path]],
        *,
        target_dir: Path,
        target_label: str,
    ) -> dict:
        return {
            "phase": "prepared",
            "target_label": target_label,
            "target_dir": str(Path(target_dir)),
            "artifacts": {
                role: {
                    "source": str(path),
                    "name": path.name,
                    "sha256": sha256_file(path),
                }
                for role, path in artifacts
            },
            "prepared_at": _now_iso(),
        }

    @staticmethod
    def _update_quality_sidecar(
        artifacts: list[tuple[str, Path]],
        quality: dict,
        metadata: dict,
        review_record: dict,
    ) -> None:
        for _role, path in artifacts:
            if path.suffix.lower() != ".json" or "quality" not in path.name.casefold():
                continue
            payload = dict(quality or {})
            try:
                current = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(current, dict):
                    payload = {**current, **payload}
            except Exception:
                pass
            payload["human_review"] = review_record
            payload["reviewed_metadata"] = metadata
            _atomic_json_write(path, payload)

    def _claimed_artifact_items(
        self,
        item: dict,
        *,
        item_id: int,
        claim_token: str,
        payload: dict,
        require_manifest: bool,
    ) -> list[tuple[str, Path]]:
        try:
            artifacts = self._artifact_items(item, require_all=True)
            if not artifacts:
                raise ReviewResolutionError(
                    "Das Review-Paket enthält keine wiederherstellbaren Dateien."
                )
            if require_manifest:
                manifest_path = next(
                    (
                        path
                        for _role, path in artifacts
                        if path.name.endswith("_job_manifest.json")
                    ),
                    None,
                )
                if manifest_path is None:
                    raise ReviewResolutionError(
                        "Das moderne Review-Paket besitzt kein Archivmanifest und darf nicht publiziert werden."
                    )
                try:
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                except Exception as exc:
                    raise ReviewResolutionError(
                        f"Das Archivmanifest des Review-Pakets ist nicht lesbar: {exc}"
                    ) from exc
                if not isinstance(manifest, dict) or str(manifest.get("job_id") or "") != str(
                    item.get("job_id") or ""
                ):
                    raise ReviewResolutionError(
                        "Das Archivmanifest gehört nicht eindeutig zum Review-Auftrag."
                    )
            return artifacts
        except Exception as exc:
            try:
                self.store.update_review_item(
                    item_id,
                    status="failed",
                    claim_token=claim_token,
                    payload=payload,
                    artifacts=item.get("artifacts") or {},
                    error=str(exc),
                )
            except Exception:
                pass
            if isinstance(exc, ReviewResolutionError):
                raise
            raise ReviewResolutionError(str(exc)) from exc

    def _review_requires_manifest(self, item: dict) -> bool:
        """Require manifests for rows created by the current durable pipeline.

        Legacy imported queue rows can predate job manifests.  Current jobs
        carry an explicit payload marker and must keep their manifest as a
        first-class package member throughout review and publication.
        """
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        return payload.get("manifest_required") is True

    def _artifact_items(self, item: dict, *, require_all: bool = False) -> list[tuple[str, Path]]:
        raw = item.get("artifacts")
        if isinstance(raw, dict):
            candidates = [(str(role), value) for role, value in raw.items() if value]
        elif isinstance(raw, list):
            candidates = [(f"artifact_{index + 1}", value) for index, value in enumerate(raw) if value]
        else:
            candidates = []

        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        recovered = self._recover_move_intent_artifacts(payload)
        staging_dir = self._safe_final_directory(payload.get("staging_dir"))

        result: list[tuple[str, Path]] = []
        missing: list[Path] = []
        seen: set[Path] = set()
        for role, raw_path in candidates:
            path = Path(raw_path)
            if not path.is_file() and role in recovered:
                path = recovered[role]
            if path.is_file():
                result.append((role, path))
                seen.add(path.resolve(strict=False))
            else:
                missing.append(path)

        # Older rows may have no role mapping.  A job-specific staging folder
        # is a safe fallback once containment below final/ has been verified.
        if not candidates and staging_dir:
            for index, path in enumerate(sorted(staging_dir.iterdir())):
                if path.is_file():
                    result.append((f"artifact_{index + 1}", path))
                    seen.add(path.resolve(strict=False))
        for role, path in recovered.items():
            resolved = path.resolve(strict=False)
            if resolved not in seen and not any(existing_role == role for existing_role, _ in result):
                result.append((role, path))
                seen.add(resolved)

        if require_all and missing:
            raise ReviewResolutionError(
                "Review-Artefakt fehlt und konnte nicht aus dem Move-Journal rekonstruiert werden: "
                + ", ".join(str(path) for path in missing)
            )
        return result

    def _safe_final_directory(self, raw_path: Any) -> Path | None:
        if not raw_path:
            return None
        final_root = Path(self.config.final_dir).resolve(strict=False)
        candidate = Path(str(raw_path))
        if not candidate.is_absolute():
            candidate = final_root / candidate
        resolved = candidate.resolve(strict=False)
        try:
            resolved.relative_to(final_root)
        except ValueError:
            return None
        return resolved if resolved.is_dir() else None

    def _recover_move_intent_artifacts(self, payload: dict) -> dict[str, Path]:
        recovered: dict[str, Path] = {}
        used: set[Path] = set()
        for intent_key in ("move_intent", "evidence_move_intent"):
            intent = payload.get(intent_key) if isinstance(payload.get(intent_key), dict) else {}
            specs = intent.get("artifacts") if isinstance(intent.get("artifacts"), dict) else {}
            target_dir = self._safe_final_directory(intent.get("target_dir"))
            if not specs or not target_dir:
                continue
            files = [path for path in target_dir.iterdir() if path.is_file()]
            for role, raw_spec in specs.items():
                spec = raw_spec if isinstance(raw_spec, dict) else {}
                expected_hash = str(spec.get("sha256") or "")
                expected_name = str(spec.get("name") or "")
                suffix = Path(expected_name).suffix.casefold()
                ordered = sorted(
                    files,
                    key=lambda path: (
                        path.name != expected_name,
                        path.suffix.casefold() != suffix,
                        path.name.casefold(),
                    ),
                )
                for path in ordered:
                    resolved = path.resolve(strict=False)
                    if resolved in used or (suffix and path.suffix.casefold() != suffix):
                        continue
                    try:
                        if expected_hash and sha256_file(path) != expected_hash:
                            continue
                    except OSError:
                        continue
                    if not expected_hash and path.name != expected_name:
                        continue
                    recovered[str(role)] = path
                    used.add(resolved)
                    break
        return recovered

    @staticmethod
    def _normalise_sync_audit(value: list[dict] | dict | None) -> list[dict]:
        if value is None:
            return []
        items = value if isinstance(value, list) else [value]
        return [
            dict(entry)
            if isinstance(entry, dict)
            else {"action": "failed", "error": f"Ungültiger Sync-Audit: {entry!r}"}
            for entry in items
        ]

    def _finish_external_audit(
        self,
        item_id: int,
        item: dict,
        target_path: str,
        artifacts: dict,
        metadata: dict,
        quality: dict,
    ) -> list[str]:
        """Write the readable JSONL mirror after the authoritative DB commit."""
        warnings: list[str] = []
        job_id = str(item.get("job_id") or "")
        if not job_id:
            return warnings
        job = self.store.get_job(job_id) or {}
        final_name = job.get("final_name") or item.get("source_name") or ""
        source_name = job.get("source_name") or item.get("source_name") or ""
        try:
            JobHistory(self.config).append_once(
                {
                    "event": "finished",
                    "job_id": job_id,
                    "status": "completed_after_review",
                    "source_name": source_name,
                    "final_name": final_name,
                    "target_path": target_path,
                    "metadata": metadata,
                    "artifacts": artifacts,
                    "quality": quality,
                },
                idempotency_key=f"review:{item_id}:completed_after_review",
            )
        except Exception as exc:
            warnings.append(f"Job-History: {exc}")
        return warnings

    def _learn_confirmed_decision(
        self,
        item: dict,
        target_path: str,
        metadata: dict,
        *,
        decision_id: str | None = None,
    ) -> None:
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        classification = payload.get("classification") if isinstance(payload.get("classification"), dict) else {}
        ClassificationMemory(self.config.base_dir).record_decision(
            chosen_path=target_path,
            fused_text=str(payload.get("fused_text") or ""),
            metadata=metadata,
            proposed_path=item.get("proposed_path") or "",
            candidates=classification.get("candidates") or item.get("candidates") or [],
            source="review",
            confirmed=True,
            decision_id=decision_id,
        )

    @staticmethod
    def _update_manifest(
        paths: list[Path],
        artifacts: dict,
        review_record: dict,
        metadata: dict,
        quality: dict,
        *,
        status: str,
        sync_phase: str,
        sync_audit: list[dict],
        require_manifest: bool = False,
    ) -> None:
        manifest_path = next(
            (path for path in paths if path.name.endswith("_job_manifest.json")),
            None,
        )
        if not manifest_path or not manifest_path.is_file():
            if require_manifest:
                raise ReviewResolutionError(
                    "Das Job-Manifest fehlt nach der Paketablage; der Review bleibt wiederaufnehmbar."
                )
            return
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            data["status"] = status
            data["review"] = review_record
            data["metadata"] = metadata
            data["quality"] = quality
            data["review_artifacts"] = dict(artifacts)
            sync = data.get("sync") if isinstance(data.get("sync"), dict) else {}
            data["sync"] = {
                **sync,
                "review": {
                    "phase": sync_phase,
                    "uploads": list(sync_audit),
                    "updated_at": _now_iso(),
                },
            }
            moved_by_name = {Path(value).name: value for value in artifacts.values()}
            previous_outputs = data.get("outputs") if isinstance(data.get("outputs"), dict) else {}
            reconciled_outputs = {}
            for role, previous in previous_outputs.items():
                if role in artifacts:
                    reconciled_outputs[role] = artifacts[role]
                elif previous:
                    reconciled_outputs[role] = moved_by_name.get(Path(previous).name, previous)
                else:
                    reconciled_outputs[role] = None
            reconciled_outputs.update(
                {
                    role: value
                    for role, value in artifacts.items()
                    if role not in reconciled_outputs
                    and not Path(value).name.endswith(("_job_manifest.json", "_debug_report.json"))
                }
            )
            data["outputs"] = reconciled_outputs
            data["output_integrity"] = {
                role: {
                    "path": value,
                    "exists": Path(value).is_file(),
                    "size_bytes": Path(value).stat().st_size if Path(value).is_file() else None,
                    "sha256": sha256_file(Path(value)) if Path(value).is_file() else None,
                }
                for role, value in reconciled_outputs.items()
                if value
            }
            data["finalized_at"] = _now_iso()
            data["updated_at"] = _now_iso()
            _atomic_json_write(manifest_path, data)
        except Exception as exc:
            raise ReviewResolutionError(
                f"Das Archivmanifest konnte nach der Prüfung nicht aktualisiert werden: {exc}"
            ) from exc

    def _remove_empty_staging_parents(self, original_artifacts: list[tuple[str, Path]]) -> None:
        staging_root = (Path(self.config.final_dir) / "_staging").resolve(strict=False)
        for _role, original in original_artifacts:
            parent = original.parent
            try:
                parent.resolve(strict=False).relative_to(staging_root)
            except ValueError:
                continue
            while parent != staging_root and parent.exists():
                try:
                    parent.rmdir()
                except OSError:
                    break
                parent = parent.parent
        if staging_root.exists():
            try:
                staging_root.rmdir()
            except OSError:
                pass
