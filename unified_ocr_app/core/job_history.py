"""Append-only processing history for support and user traceability."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from core.cache import sha256_file
from core.local_store import LocalStore


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobHistory:
    """Stores compact JSONL records in the workspace log directory."""

    def __init__(self, config):
        self.config = config
        self.path = Path(config.log_dir) / "job_history.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.store = None
        if hasattr(config, "base_dir"):
            try:
                self.store = LocalStore(config)
            except Exception:
                self.store = None

    def append(self, record: dict) -> None:
        safe_record = dict(record)
        safe_record.setdefault("timestamp_utc", _now())
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(safe_record, ensure_ascii=False, default=str) + "\n")

    def start(self, source_path: Path) -> str:
        job_id = uuid.uuid4().hex
        source_sha256 = sha256_file(source_path)
        self.append({
            "event": "started",
            "job_id": job_id,
            "source_name": Path(source_path).name,
            "source_path": str(source_path),
            "source_sha256": source_sha256,
        })
        if self.store:
            self.store.start_job(job_id, source_path, source_sha256)
        return job_id

    def finish(
        self,
        job_id: str,
        status: str,
        *,
        source_name: str,
        final_name: str = "",
        target_path: str = "",
        error: str = "",
        metadata: dict | None = None,
    ) -> None:
        self.append({
            "event": "finished",
            "job_id": job_id,
            "status": status,
            "source_name": source_name,
            "final_name": final_name,
            "target_path": target_path,
            "error": error,
            "metadata": metadata or {},
        })
        if self.store:
            self.store.update_job(
                job_id,
                status,
                final_name=final_name,
                target_path=target_path,
                error=error,
                metadata=metadata or {},
            )
