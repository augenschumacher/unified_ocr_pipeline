"""Collision-safe, auditable organization of complete document packages."""

from __future__ import annotations

import logging
import os
import shutil
import uuid
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.cache import sha256_file
from core.cloud.folder_registry import (
    UnsafeArchivePath,
    normalize_archive_path,
    resolve_archive_target,
)

logger = logging.getLogger("UnifiedOCR")

SUPPORTED_ORGANIZE_SUFFIXES = {".pdf", ".txt", ".docx", ".odt", ".doc", ".odoc"}

# Sidecars use the document basename plus a role suffix. Keeping the suffix
# separate from the package stem lets a collision rename the complete package
# consistently (``doc_conflict_...pdf`` and
# ``doc_conflict_..._quality_report.json``).
KNOWN_ARTIFACT_ROLE_SUFFIXES = tuple(sorted({
    "_quality_report",
    "_job_manifest",
    "_debug_report",
    "_ocr_report",
    "_metadata",
    "_manifest",
    "_quality",
}, key=len, reverse=True))


class PackageMoveError(RuntimeError):
    """Raised when a document package could not be moved as one transaction."""

    def __init__(self, message: str, *, audit: list[dict] | None = None):
        super().__init__(message)
        self.audit = list(audit or [])


class DocumentOrganizer:
    """Move document artifacts below ``final`` without overwriting files.

    New callers should pass concrete paths to :meth:`organize_artifacts`.  The
    legacy :meth:`organize` method remains available, but its scan only accepts
    the exact basename (``final_name + supported extension``); a similarly
    prefixed document can therefore never be pulled into the wrong package.
    """

    def __init__(self, final_dir: Path):
        self.final_dir = Path(final_dir)
        self.last_audit: list[dict] = []

    def organize(
        self,
        final_name: str,
        target_path: str,
        artifact_paths: Iterable[Path] | Mapping[str, Path] | None = None,
    ) -> list[Path]:
        """Organize a package, retaining compatibility with the legacy API.

        Supplying ``artifact_paths`` activates the strict explicit-artifact
        path.  Without it, only exact basename matches in the top-level final
        directory are considered.  Unsafe legacy targets are deliberately
        routed to ``Sonstiges`` and recorded in the audit; the new explicit API
        rejects them with :class:`UnsafeArchivePath`.
        """
        if artifact_paths is not None:
            return self.organize_artifacts(artifact_paths, target_path, package_id=final_name)

        self.last_audit = []
        target_dir = self._legacy_target_dir(target_path)
        if not self.final_dir.exists():
            return []

        artifacts = sorted(
            (
                item
                for item in self.final_dir.iterdir()
                if self._is_matching_export(item, final_name)
            ),
            key=lambda item: item.name.casefold(),
        )
        if not artifacts:
            return []
        try:
            return self.move_artifacts_to_directory(
                artifacts,
                target_dir,
                target_label=normalize_archive_path(
                    target_dir.resolve().relative_to(self.final_dir.resolve()).as_posix()
                ),
                package_id=final_name,
            )
        except Exception as exc:
            logger.error("Dokumentpaket %s konnte nicht verschoben werden: %s", final_name, exc)
            if not any(entry.get("action") == "package_rolled_back" for entry in self.last_audit):
                self.last_audit.append({
                    "action": "move_failed",
                    "package_id": final_name,
                    "target_dir": str(target_dir),
                    "error": str(exc),
                })
            return []

    def organize_artifacts(
        self,
        artifact_paths: Iterable[Path] | Mapping[str, Path],
        target_path: str,
        *,
        package_id: str = "",
    ) -> list[Path]:
        """Strictly move explicitly supplied artifacts to a relative target.

        Every source is validated before any directory is created or file is
        moved.  Each file publication is an atomic filesystem operation and a
        failure rolls the complete package back to its original paths.
        """
        self.last_audit = []
        try:
            target_dir = resolve_archive_target(self.final_dir, target_path)
        except UnsafeArchivePath as exc:
            self.last_audit.append({
                "action": "target_path_rejected",
                "input": str(target_path),
                "error": str(exc),
            })
            raise
        return self.move_artifacts_to_directory(
            artifact_paths,
            target_dir,
            target_label=normalize_archive_path(target_path),
            package_id=package_id,
        )

    # Alias with package-oriented wording for integration code.
    move_package = organize_artifacts

    def move_file_to_directory(
        self,
        source: Path,
        target_dir: Path,
        *,
        target_label: str = "",
    ) -> Path:
        """Move one file using the same transactional machinery as a package."""
        return self.move_artifacts_to_directory(
            [source],
            target_dir,
            target_label=target_label,
            package_id=Path(source).stem,
        )[0]

    def move_artifacts_to_directory(
        self,
        artifact_paths: Iterable[Path] | Mapping[str, Path],
        target_dir: Path,
        *,
        target_label: str = "",
        package_id: str = "",
    ) -> list[Path]:
        """Move all supplied files as a rollback-capable package transaction."""
        target = self._assert_inside_final(Path(target_dir))
        artifacts = self._normalize_artifacts(artifact_paths)
        if not artifacts:
            return []

        package_token = uuid.uuid4().hex
        public_package_id = str(package_id or package_token)
        plans = self._plan_package(artifacts, target, package_token)
        actionable = [plan for plan in plans if plan["action"] != "already_in_place"]
        transaction_dir = self._assert_inside_final(
            self.final_dir.resolve() / "_transactions" / package_token
        )

        self.last_audit.append({
            "action": "package_started",
            "package_id": public_package_id,
            "transaction_id": package_token,
            "target": target_label,
            "target_dir": str(target),
            "artifacts": [str(plan["source"]) for plan in plans],
            "created_at": self._now(),
        })

        staged: list[dict[str, Any]] = []
        committed: list[dict[str, Any]] = []
        keep_transaction_for_recovery = False
        try:
            # All containment and destination checks above intentionally happen
            # before the first mkdir.
            target.mkdir(parents=True, exist_ok=True)
            for plan in actionable:
                Path(plan["physical_destination"]).parent.mkdir(parents=True, exist_ok=True)
            if actionable:
                transaction_dir.mkdir(parents=True, exist_ok=False)

            for index, plan in enumerate(actionable, start=1):
                stage_path = transaction_dir / f"artifact_{index:04d}{Path(plan['source']).suffix}"
                os.replace(plan["source"], stage_path)
                plan["stage_path"] = stage_path
                staged.append(plan)

            for plan in actionable:
                self._publish_without_overwrite(
                    Path(plan["stage_path"]),
                    Path(plan["physical_destination"]),
                )
                committed.append(plan)

            results: list[Path] = []
            for plan in plans:
                results.append(Path(plan["result_destination"]))
                audit_entry = {
                    "action": plan["action"],
                    "source": str(plan["source"]),
                    "destination": str(plan["result_destination"]),
                    "target": target_label,
                    "artifact_role": plan["role"],
                    "sha256": plan["sha256"],
                    "transaction_id": package_token,
                }
                if plan["action"] == "duplicate_kept_existing":
                    audit_entry["existing_destination"] = str(plan["result_destination"])
                    audit_entry["recovery_path"] = str(plan["physical_destination"])
                self.last_audit.append(audit_entry)

            self.last_audit.append({
                "action": "package_committed",
                "package_id": public_package_id,
                "transaction_id": package_token,
                "artifact_count": len(plans),
                "destinations": [str(path) for path in results],
                "completed_at": self._now(),
            })
            return results
        except Exception as exc:
            rollback_errors = self._rollback_package(staged, committed)
            keep_transaction_for_recovery = bool(rollback_errors)
            self.last_audit.append({
                "action": "package_rolled_back",
                "package_id": public_package_id,
                "transaction_id": package_token,
                "error": str(exc),
                "rollback_errors": rollback_errors,
                "completed_at": self._now(),
            })
            detail = f"Dokumentpaket wurde zurückgerollt: {exc}"
            if rollback_errors:
                detail += f"; Rollback-Fehler: {'; '.join(rollback_errors)}"
            raise PackageMoveError(detail, audit=self.last_audit) from exc
        finally:
            if not keep_transaction_for_recovery:
                self._remove_transaction_dir(transaction_dir)

    def _normalize_artifacts(
        self,
        artifact_paths: Iterable[Path] | Mapping[str, Path],
    ) -> list[tuple[str, Path]]:
        if isinstance(artifact_paths, Mapping):
            raw_items = list(artifact_paths.items())
        else:
            if isinstance(artifact_paths, (str, bytes, os.PathLike)):
                raise TypeError("artifact_paths muss eine Sammlung konkreter Pfade sein.")
            raw_items = [("", value) for value in artifact_paths]

        normalized: list[tuple[str, Path]] = []
        seen: set[Path] = set()
        final_root = self.final_dir.resolve()
        for role, raw_path in raw_items:
            if raw_path is None:
                raise ValueError(f"Artefaktpfad für {role!r} fehlt.")
            source_input = Path(raw_path)
            if not source_input.is_absolute():
                source_input = final_root / source_input
            if source_input.is_symlink():
                raise ValueError(f"Symbolische Links sind keine zulässigen Artefakte: {source_input}")
            try:
                source = source_input.resolve(strict=True)
                relative = source.relative_to(final_root)
            except FileNotFoundError as exc:
                raise FileNotFoundError(f"Artefakt existiert nicht: {source_input}") from exc
            except ValueError as exc:
                raise ValueError(f"Artefakt liegt außerhalb des final-Ordners: {source_input}") from exc
            if not source.is_file():
                raise ValueError(f"Artefakt ist keine reguläre Datei: {source}")
            normalize_archive_path(relative.as_posix())
            normalize_archive_path(source.name)
            if source in seen:
                raise ValueError(f"Artefakt wurde mehrfach angegeben: {source}")
            seen.add(source)
            normalized.append((str(role), source))
        return normalized

    def _plan_package(
        self,
        artifacts: list[tuple[str, Path]],
        target_dir: Path,
        transaction_id: str,
    ) -> list[dict[str, Any]]:
        plans: list[dict[str, Any]] = []
        reserved: set[Path] = set()
        stamp = self._timestamp()
        recovery_dir = self._assert_inside_final(
            self.final_dir.resolve() / "_recovery" / stamp / transaction_id / "duplicates"
        )

        layouts = self._package_layouts(artifacts)
        prepared: list[dict[str, Any]] = []
        desired_sources: dict[Path, Path] = {}
        for order, ((role, source), (package_stem, role_suffix)) in enumerate(
            zip(artifacts, layouts)
        ):
            desired = target_dir / source.name
            other_source = desired_sources.get(desired)
            if other_source is not None:
                raise ValueError(
                    "Mehrere Artefakte würden denselben Zielnamen verwenden: "
                    f"{other_source} und {source} -> {desired.name}"
                )
            desired_sources[desired] = source
            prepared.append({
                "role": role,
                "source": source,
                "sha256": sha256_file(source),
                "desired": desired,
                "package_stem": package_stem,
                "role_suffix": role_suffix,
                "order": order,
            })

        groups: dict[str, list[dict[str, Any]]] = {}
        for item in prepared:
            groups.setdefault(item["package_stem"], []).append(item)

        for package_stem, members in groups.items():
            # Identical existing files are idempotent duplicates, not name
            # conflicts. A single genuinely conflicting member, however,
            # renames every member of this logical package. This prevents a
            # PDF and its sidecars from being split between two basenames.
            conflicting = []
            for item in members:
                desired = Path(item["desired"])
                source = Path(item["source"])
                if self._paths_equivalent(source, desired) or not desired.exists():
                    continue
                if not desired.is_file() or sha256_file(desired) != item["sha256"]:
                    conflicting.append(item)

            conflict_destinations: dict[Path, Path] = {}
            if conflicting:
                conflict_stem = self._unique_package_conflict_stem(
                    target_dir,
                    package_stem,
                    members,
                    reserved,
                    stamp=stamp,
                )
                for item in members:
                    destination = target_dir / (
                        f"{conflict_stem}{item['role_suffix']}{Path(item['source']).suffix}"
                    )
                    conflict_destinations[Path(item["source"])] = destination
                    reserved.add(destination)
                for item in conflicting:
                    source = Path(item["source"])
                    self.last_audit.append({
                        "action": "name_conflict",
                        "source": str(source),
                        "existing_destination": str(item["desired"]),
                        "new_destination": str(conflict_destinations[source]),
                        "package_stem": package_stem,
                        "conflict_stem": conflict_stem,
                        "affected_artifacts": len(members),
                        "message": (
                            "Bestehende Datei wurde nicht überschrieben; das gesamte "
                            "Dokumentpaket erhält einen gemeinsamen Konfliktnamen."
                        ),
                    })

            for item in members:
                role = item["role"]
                source = Path(item["source"])
                source_hash = item["sha256"]
                desired = Path(item["desired"])

                if conflicting:
                    destination = conflict_destinations[source]
                    action = "moved_with_conflict_name"
                elif self._paths_equivalent(source, desired):
                    destination = desired
                    action = "already_in_place"
                    reserved.add(desired)
                elif desired.exists() and desired.is_file() and sha256_file(desired) == source_hash:
                    recovery_path = self._unique_path(recovery_dir / source.name, reserved)
                    plans.append({
                        "role": role,
                        "source": source,
                        "sha256": source_hash,
                        "action": "duplicate_kept_existing",
                        "physical_destination": recovery_path,
                        "result_destination": desired,
                        "_order": item["order"],
                    })
                    reserved.update({desired, recovery_path})
                    continue
                else:
                    destination = desired
                    action = "moved"
                    reserved.add(destination)

                plans.append({
                    "role": role,
                    "source": source,
                    "sha256": source_hash,
                    "action": action,
                    "physical_destination": destination,
                    "result_destination": destination,
                    "_order": item["order"],
                })
        plans.sort(key=lambda plan: plan["_order"])
        for plan in plans:
            plan.pop("_order", None)
        return plans

    @classmethod
    def _package_layouts(
        cls,
        artifacts: list[tuple[str, Path]],
    ) -> list[tuple[str, str]]:
        """Return ``(package_stem, role_suffix)`` for every artifact."""
        stems = [source.stem for _role, source in artifacts]
        if not stems:
            return []
        if len(stems) == 1:
            return [(stems[0], "")]

        common = os.path.commonprefix(stems)
        if common not in stems:
            common = common.rstrip("_")
            if common and not all(
                stem == common or stem.startswith(f"{common}_")
                for stem in stems
            ):
                common = common.rsplit("_", 1)[0] if "_" in common else ""
        if common and all(
            stem == common or stem.startswith(f"{common}_")
            for stem in stems
        ):
            return [(common, stem[len(common):]) for stem in stems]

        layouts: list[tuple[str, str]] = []
        for stem in stems:
            package_stem = stem
            role_suffix = ""
            for suffix in KNOWN_ARTIFACT_ROLE_SUFFIXES:
                if stem.endswith(suffix) and len(stem) > len(suffix):
                    package_stem = stem[:-len(suffix)]
                    role_suffix = suffix
                    break
            layouts.append((package_stem, role_suffix))
        return layouts

    @staticmethod
    def _unique_package_conflict_stem(
        target_dir: Path,
        package_stem: str,
        members: list[dict[str, Any]],
        reserved: set[Path],
        *,
        stamp: str,
    ) -> str:
        """Reserve one available conflict stem for every member in a package."""
        base = f"{package_stem}_conflict_{stamp}"
        counter = 1
        while True:
            candidate_stem = base if counter == 1 else f"{base}_{counter}"
            candidates = [
                target_dir / (
                    f"{candidate_stem}{item['role_suffix']}{Path(item['source']).suffix}"
                )
                for item in members
            ]
            if len(set(candidates)) != len(candidates):
                raise ValueError(
                    "Artefakte eines Dokumentpakets erzeugen identische Konfliktnamen."
                )
            if all(not candidate.exists() and candidate not in reserved for candidate in candidates):
                return candidate_stem
            counter += 1

    @staticmethod
    def _paths_equivalent(left: Path, right: Path) -> bool:
        """Compare paths after Windows/case and symlink normalization."""
        return left == right or left.resolve(strict=False) == right.resolve(strict=False)

    def _rollback_package(self, staged: list[dict], committed: list[dict]) -> list[str]:
        errors: list[str] = []
        committed_ids = {id(plan) for plan in committed}
        for plan in reversed(committed):
            try:
                destination = Path(plan["physical_destination"])
                source = Path(plan["source"])
                if source.exists():
                    raise FileExistsError(f"Ursprungspfad ist beim Rollback belegt: {source}")
                self._publish_without_overwrite(destination, source)
            except Exception as rollback_exc:
                errors.append(str(rollback_exc))
        for plan in reversed(staged):
            if id(plan) in committed_ids:
                continue
            try:
                stage_path = Path(plan["stage_path"])
                source = Path(plan["source"])
                if stage_path.exists():
                    if source.exists():
                        raise FileExistsError(f"Ursprungspfad ist beim Rollback belegt: {source}")
                    self._publish_without_overwrite(stage_path, source)
            except Exception as rollback_exc:
                errors.append(str(rollback_exc))
        return errors

    @staticmethod
    def _publish_without_overwrite(staged: Path, destination: Path) -> None:
        """Atomically publish *staged* while refusing an existing destination."""
        try:
            os.link(staged, destination)
            staged.unlink()
            return
        except FileExistsError:
            raise
        except OSError:
            # Reserve the name exclusively before the atomic replace.  This
            # fallback is for filesystems without hard-link support.
            flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
            descriptor = os.open(destination, flags, 0o600)
            try:
                reservation = os.fstat(descriptor)
            finally:
                os.close(descriptor)
            current = destination.stat()
            if (current.st_dev, current.st_ino, current.st_size) != (
                reservation.st_dev,
                reservation.st_ino,
                0,
            ):
                raise FileExistsError(f"Zieldatei wurde zwischenzeitlich verändert: {destination}")
            try:
                os.replace(staged, destination)
            except Exception:
                try:
                    current = destination.stat()
                    if (current.st_dev, current.st_ino, current.st_size) == (
                        reservation.st_dev,
                        reservation.st_ino,
                        0,
                    ):
                        destination.unlink()
                except OSError:
                    pass
                raise

    def _legacy_target_dir(self, target_path: str) -> Path:
        try:
            return resolve_archive_target(self.final_dir, target_path or "Sonstiges")
        except UnsafeArchivePath as exc:
            logger.warning("Unsicherer Legacy-Zielpfad verworfen: %s", target_path)
            self.last_audit.append({
                "action": "target_path_sanitized",
                "input": str(target_path),
                "output": "Sonstiges",
                "error": str(exc),
            })
            return resolve_archive_target(self.final_dir, "Sonstiges")

    def _target_dir(self, target_path: str) -> Path:
        """Strict target resolver retained for compatibility with older callers."""
        return resolve_archive_target(self.final_dir, target_path)

    def _clean_target_path(self, target_path: str) -> str:
        """Strict path normalizer retained for compatibility with older callers."""
        return normalize_archive_path(target_path, default="Sonstiges")

    def _assert_inside_final(self, path: Path) -> Path:
        final_display = self.final_dir.absolute()
        final_root = final_display.resolve()
        candidate_input = path if path.is_absolute() else final_display / path
        candidate = candidate_input.resolve(strict=False)
        try:
            relative = candidate.relative_to(final_root)
        except ValueError as exc:
            raise UnsafeArchivePath(
                f"Zielpfad liegt außerhalb des final-Ordners: {candidate}"
            ) from exc
        if relative.parts:
            normalize_archive_path(relative.as_posix())
        return candidate_input.absolute()

    def _is_matching_export(self, item: Path, final_name: str) -> bool:
        return (
            item.is_file()
            and item.suffix.lower() in SUPPORTED_ORGANIZE_SUFFIXES
            and item.stem == final_name
        )

    def _unique_conflict_path(
        self,
        desired: Path,
        reserved: set[Path] | None = None,
        *,
        stamp: str | None = None,
    ) -> Path:
        candidate = desired.with_name(
            f"{desired.stem}_conflict_{stamp or self._timestamp()}{desired.suffix}"
        )
        return self._unique_path(candidate, reserved)

    @staticmethod
    def _unique_path(desired: Path, reserved: set[Path] | None = None) -> Path:
        reserved = reserved or set()
        if not desired.exists() and desired not in reserved:
            return desired
        counter = 2
        while True:
            candidate = desired.with_name(f"{desired.stem}_{counter}{desired.suffix}")
            if not candidate.exists() and candidate not in reserved:
                return candidate
            counter += 1

    @staticmethod
    def _remove_transaction_dir(transaction_dir: Path) -> None:
        if transaction_dir.exists():
            shutil.rmtree(transaction_dir, ignore_errors=True)
        parent = transaction_dir.parent
        try:
            parent.rmdir()
        except OSError:
            pass

    @staticmethod
    def _timestamp() -> str:
        return datetime.now().strftime("%Y%m%d_%H%M%S_%f")

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()
