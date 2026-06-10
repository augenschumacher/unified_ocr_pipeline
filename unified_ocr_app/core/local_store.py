"""Local SQLite store for jobs, document index, duplicates, and review queue."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class LocalStore:
    def __init__(self, config_or_base_dir):
        base_dir = getattr(config_or_base_dir, "base_dir", config_or_base_dir)
        self.base_dir = Path(base_dir)
        self.db_path = self.base_dir / "unified_ocr.sqlite3"
        self._init_db()

    def _connect(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    source_name TEXT,
                    source_path TEXT,
                    source_sha256 TEXT,
                    final_name TEXT,
                    target_path TEXT,
                    metadata_json TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS job_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT,
                    event TEXT NOT NULL,
                    stage TEXT,
                    status TEXT,
                    payload_json TEXT,
                    created_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS documents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_sha256 TEXT,
                    source_name TEXT,
                    final_name TEXT,
                    target_path TEXT,
                    outputs_json TEXT,
                    metadata_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_documents_sha ON documents(source_sha256)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_documents_target ON documents(target_path)")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS review_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    source_name TEXT,
                    proposed_path TEXT,
                    chosen_path TEXT,
                    candidates_json TEXT,
                    metadata_json TEXT,
                    created_at TEXT NOT NULL,
                    resolved_at TEXT
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_review_status ON review_queue(status)")

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value or {}, ensure_ascii=False, default=str)

    def start_job(self, job_id: str, source_path: Path, source_sha256: str | None = None):
        now = _now()
        with self._connect() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO jobs (
                    job_id, status, source_name, source_path, source_sha256, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, COALESCE((SELECT created_at FROM jobs WHERE job_id = ?), ?), ?)
            """, (
                job_id,
                "started",
                Path(source_path).name,
                str(source_path),
                source_sha256,
                job_id,
                now,
                now,
            ))
            conn.execute("""
                INSERT INTO job_events (job_id, event, status, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?)
            """, (job_id, "started", "started", self._json({"source_path": str(source_path), "source_sha256": source_sha256}), now))

    def update_job(self, job_id: str, status: str, **fields):
        now = _now()
        allowed = {"source_sha256", "final_name", "target_path", "metadata_json", "error"}
        values = {key: value for key, value in fields.items() if key in allowed}
        if "metadata" in fields:
            values["metadata_json"] = self._json(fields["metadata"])
        assignments = ", ".join([f"{key} = ?" for key in values])
        params = list(values.values())
        with self._connect() as conn:
            if assignments:
                conn.execute(
                    f"UPDATE jobs SET status = ?, {assignments}, updated_at = ? WHERE job_id = ?",
                    [status, *params, now, job_id],
                )
            else:
                conn.execute("UPDATE jobs SET status = ?, updated_at = ? WHERE job_id = ?", (status, now, job_id))
            conn.execute("""
                INSERT INTO job_events (job_id, event, status, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?)
            """, (job_id, "status", status, self._json(fields), now))

    def record_event(self, job_id: str, event: str, *, stage: str = "", status: str = "", payload: dict | None = None):
        with self._connect() as conn:
            conn.execute("""
                INSERT INTO job_events (job_id, event, stage, status, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (job_id, event, stage, status, self._json(payload), _now()))

    def find_duplicates(self, source_sha256: str | None, limit: int = 5) -> list[dict]:
        if not source_sha256:
            return []
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT source_name, final_name, target_path, outputs_json, metadata_json, updated_at
                FROM documents
                WHERE source_sha256 = ?
                ORDER BY updated_at DESC
                LIMIT ?
            """, (source_sha256, limit)).fetchall()
        return [dict(row) for row in rows]

    def index_document(
        self,
        *,
        source_sha256: str | None,
        source_name: str,
        final_name: str,
        target_path: str,
        outputs: dict,
        metadata: dict,
    ):
        now = _now()
        with self._connect() as conn:
            conn.execute("""
                INSERT INTO documents (
                    source_sha256, source_name, final_name, target_path, outputs_json, metadata_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                source_sha256,
                source_name,
                final_name,
                target_path,
                self._json(outputs),
                self._json(metadata),
                now,
                now,
            ))

    def add_review_item(
        self,
        *,
        job_id: str,
        kind: str,
        source_name: str,
        proposed_path: str,
        candidates: list[dict] | None = None,
        metadata: dict | None = None,
    ) -> int:
        with self._connect() as conn:
            cursor = conn.execute("""
                INSERT INTO review_queue (
                    job_id, kind, status, source_name, proposed_path, candidates_json, metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                job_id,
                kind,
                "pending",
                source_name,
                proposed_path,
                self._json(candidates or []),
                self._json(metadata or {}),
                _now(),
            ))
            return int(cursor.lastrowid)

    def resolve_review_item(self, item_id: int | None, chosen_path: str):
        if not item_id:
            return
        with self._connect() as conn:
            conn.execute("""
                UPDATE review_queue
                SET status = ?, chosen_path = ?, resolved_at = ?
                WHERE id = ?
            """, ("resolved", chosen_path, _now(), item_id))

    def list_review_items(self, status: str = "pending", limit: int = 100) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT * FROM review_queue
                WHERE status = ?
                ORDER BY created_at DESC
                LIMIT ?
            """, (status, limit)).fetchall()
        return [dict(row) for row in rows]

    def search_documents(self, query: str = "", limit: int = 100) -> list[dict]:
        pattern = f"%{query}%"
        with self._connect() as conn:
            if query:
                rows = conn.execute("""
                    SELECT * FROM documents
                    WHERE final_name LIKE ? OR source_name LIKE ? OR target_path LIKE ? OR metadata_json LIKE ?
                    ORDER BY updated_at DESC
                    LIMIT ?
                """, (pattern, pattern, pattern, pattern, limit)).fetchall()
            else:
                rows = conn.execute("""
                    SELECT * FROM documents
                    ORDER BY updated_at DESC
                    LIMIT ?
                """, (limit,)).fetchall()
        return [dict(row) for row in rows]
