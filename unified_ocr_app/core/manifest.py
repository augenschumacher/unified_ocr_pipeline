"""Per-job manifest writing for auditability and crash forensics."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.cache import sha256_file


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _path_value(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _path_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_path_value(v) for v in value]
    return value


class JobManifest:
    """Small JSON manifest persisted throughout one pipeline run."""

    def __init__(self, path: Path, *, job_id: str, source_path: Path):
        self.path = Path(path)
        self.data: dict[str, Any] = {
            "schema": "unified_ocr_job_manifest_v1",
            "job_id": job_id,
            "status": "running",
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
            "source": {
                "original_path": str(source_path),
                "name": source_path.name,
                "suffix": source_path.suffix.lower(),
                "sha256": sha256_file(source_path),
            },
            "outputs": {},
            "stages": {},
            "metadata": {},
            "drive": {"enabled": False, "uploads": []},
            "sync": {"enabled": False, "targets": {}, "uploads": []},
            "warnings": [],
        }
        self.write()

    @classmethod
    def create(cls, *, job_id: str, source_path: Path, manifest_dir: Path) -> "JobManifest":
        return cls(Path(manifest_dir) / "job_manifest.json", job_id=job_id, source_path=source_path)

    def record_stage(
        self,
        name: str,
        status: str,
        *,
        warnings: list[str] | None = None,
        provenance: dict[str, Any] | None = None,
        artifacts: dict[str, Any] | None = None,
    ):
        entry = {
            "status": status,
            "updated_at": _now_iso(),
            "warnings": warnings or [],
            "provenance": _path_value(provenance or {}),
            "artifacts": _path_value(artifacts or {}),
        }
        self.data["stages"][name] = entry
        if warnings:
            self.data.setdefault("warnings", []).extend(warnings)
        self.write()

    def record_outputs(self, outputs: dict[str, Any]):
        self.data["outputs"] = {
            key: str(value) if value else None
            for key, value in (outputs or {}).items()
        }
        self.write()

    def record_metadata(self, metadata: dict[str, Any]):
        self.data["metadata"] = _path_value(metadata or {})
        self.write()

    def record_source_context(self, *, input_dir: Path | str | None = None, input_profile: str | None = None):
        if input_dir:
            self.data["source"]["input_dir"] = str(input_dir)
        if input_profile:
            self.data["source"]["input_profile"] = str(input_profile)
        self.write()

    def record_drive_uploads(self, *, enabled: bool, uploads: list[dict[str, Any]] | None):
        self.data["drive"] = {
            "enabled": bool(enabled),
            "uploads": _path_value(uploads or []),
        }
        self.write()

    def record_sync_uploads(
        self,
        *,
        enabled: bool,
        targets: dict[str, bool] | None = None,
        uploads: list[dict[str, Any]] | None = None,
    ):
        self.data["sync"] = {
            "enabled": bool(enabled),
            "targets": dict(targets or {}),
            "uploads": _path_value(uploads or []),
        }
        self.write()

    def finalize(self, status: str, *, error: str | None = None):
        self.data["status"] = status
        if error:
            self.data["error"] = error
        self.write()

    def write_copy(self, destination: Path) -> Path:
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        self.write()
        destination.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8")
        return destination

    def write(self):
        self.data["updated_at"] = _now_iso()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8")
