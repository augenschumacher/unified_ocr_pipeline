"""Migration-aware SQLite store for jobs, documents and persistent review work."""

from __future__ import annotations

import json
import hmac
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator


SCHEMA_VERSION = 5
DEFAULT_REVIEW_LEASE_SECONDS = 60 * 60
TERMINAL_JOB_STATUSES = frozenset({
    "completed",
    "completed_with_warnings",
    "completed_after_review",
    "cancelled",
})
RECOVERABLE_JOB_STATUSES = (
    "started",
    "processing",
    "staging",
    "staged",
    "review_required",
    "deferred",
    "paused",
    "failed",
    "resuming",
    "ready_to_resume",
    "sync_failed",
)
RECOVERABLE_REVIEW_STATUSES = (
    "pending",
    "in_review",
    "staged",
    "failed",
    "ready_to_resume",
)
_STATUS_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")

_JSON_ALIASES = {
    "metadata_json": "metadata",
    "outputs_json": "outputs",
    "candidates_json": "candidates",
    "payload_json": "payload",
    "artifacts_json": "artifacts",
    "quality_json": "quality",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class LocalStore:
    """Durable local state with backward-compatible, idempotent migrations."""

    def __init__(self, config_or_base_dir):
        base_dir = getattr(config_or_base_dir, "base_dir", config_or_base_dir)
        self.base_dir = Path(base_dir)
        self.db_path = self.base_dir / "unified_ocr.sqlite3"
        self._init_db()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 10000")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    applied_at TEXT NOT NULL
                )
            """)
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
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    artifacts_json TEXT NOT NULL DEFAULT '{}',
                    quality_json TEXT NOT NULL DEFAULT '{}',
                    error TEXT,
                    revision INTEGER NOT NULL DEFAULT 0,
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
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    artifacts_json TEXT NOT NULL DEFAULT '{}',
                    quality_json TEXT NOT NULL DEFAULT '{}',
                    error TEXT,
                    revision INTEGER NOT NULL DEFAULT 0,
                    resume_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT,
                    opened_at TEXT,
                    resolved_at TEXT
                )
            """)

            # Existing installations may have any earlier, unversioned schema.
            # Column discovery makes each migration safe to run repeatedly.
            self._ensure_columns(conn, "jobs", {
                "payload_json": "TEXT NOT NULL DEFAULT '{}'",
                "artifacts_json": "TEXT NOT NULL DEFAULT '{}'",
                "quality_json": "TEXT NOT NULL DEFAULT '{}'",
                "revision": "INTEGER NOT NULL DEFAULT 0",
            })
            self._ensure_columns(conn, "review_queue", {
                "payload_json": "TEXT NOT NULL DEFAULT '{}'",
                "artifacts_json": "TEXT NOT NULL DEFAULT '{}'",
                "quality_json": "TEXT NOT NULL DEFAULT '{}'",
                "error": "TEXT",
                "revision": "INTEGER NOT NULL DEFAULT 0",
                "resume_count": "INTEGER NOT NULL DEFAULT 0",
                "updated_at": "TEXT",
                "opened_at": "TEXT",
                "claim_token": "TEXT",
                "claim_expires_at": "TEXT",
            })
            conn.execute("UPDATE review_queue SET updated_at = created_at WHERE updated_at IS NULL")

            conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_updated ON jobs(updated_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_job_events_job ON job_events(job_id, created_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_documents_sha ON documents(source_sha256)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_documents_target ON documents(target_path)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_review_status ON review_queue(status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_review_job ON review_queue(job_id, status)")

            # Version 5 makes the content hash an idempotency key.  Preserve
            # the newest legacy row deterministically before adding the partial
            # uniqueness constraint; empty/unknown hashes remain unrestricted.
            conn.execute("""
                DELETE FROM documents
                WHERE source_sha256 IS NOT NULL AND TRIM(source_sha256) <> ''
                  AND EXISTS (
                    SELECT 1 FROM documents AS newer
                    WHERE newer.source_sha256 = documents.source_sha256
                      AND (
                        newer.updated_at > documents.updated_at
                        OR (
                            newer.updated_at = documents.updated_at
                            AND newer.id > documents.id
                        )
                      )
                  )
            """)
            conn.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS uq_documents_source_sha
                ON documents(source_sha256)
                WHERE source_sha256 IS NOT NULL AND TRIM(source_sha256) <> ''
            """)

            migrations = (
                (1, "base_schema"),
                (2, "persistent_job_payloads"),
                (3, "persistent_review_recovery"),
                (4, "tokenized_review_claims_and_atomic_finalization"),
                (5, "unique_document_hash_upsert"),
            )
            for version, name in migrations:
                conn.execute(
                    "INSERT OR IGNORE INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                    (version, name, _now()),
                )
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    @staticmethod
    def _ensure_columns(conn: sqlite3.Connection, table: str, definitions: dict[str, str]) -> None:
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for column, definition in definitions.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps({} if value is None else value, ensure_ascii=False, default=str)

    @staticmethod
    def _decode_json(value: Any, default: Any) -> Any:
        if value in (None, ""):
            return default
        try:
            return json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return default

    @classmethod
    def _row_dict(cls, row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        result = dict(row)
        for column, alias in _JSON_ALIASES.items():
            if column not in result:
                continue
            default = [] if column == "candidates_json" else {}
            result[alias] = cls._decode_json(result[column], default)
        return result

    @staticmethod
    def _status(value: str) -> str:
        status = str(value or "").strip().lower()
        if not _STATUS_RE.fullmatch(status):
            raise ValueError(
                "Status muss mit einem Buchstaben beginnen und darf nur a-z, 0-9, '_' und '-' enthalten."
            )
        return status

    @staticmethod
    def _bounded_limit(limit: int, *, maximum: int = 1000) -> int:
        return max(1, min(int(limit), maximum))

    # ------------------------------------------------------------------
    # Jobs
    # ------------------------------------------------------------------

    def start_job(
        self,
        job_id: str,
        source_path: Path,
        source_sha256: str | None = None,
        *,
        payload: dict | None = None,
        artifacts: dict | list | None = None,
        quality: dict | None = None,
    ) -> dict:
        job_id = str(job_id or "").strip()
        if not job_id:
            raise ValueError("job_id darf nicht leer sein.")
        now = _now()
        source = Path(source_path)
        with self._connect() as conn:
            conn.execute("""
                INSERT INTO jobs (
                    job_id, status, source_name, source_path, source_sha256,
                    payload_json, artifacts_json, quality_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    status = excluded.status,
                    source_name = excluded.source_name,
                    source_path = excluded.source_path,
                    source_sha256 = excluded.source_sha256,
                    payload_json = excluded.payload_json,
                    artifacts_json = excluded.artifacts_json,
                    quality_json = excluded.quality_json,
                    error = NULL,
                    revision = jobs.revision + 1,
                    updated_at = excluded.updated_at
            """, (
                job_id,
                "started",
                source.name,
                str(source),
                source_sha256,
                self._json(payload),
                self._json(artifacts),
                self._json(quality),
                now,
                now,
            ))
            self._insert_event(
                conn,
                job_id,
                "started",
                status="started",
                payload={
                    "source_path": str(source),
                    "source_sha256": source_sha256,
                    "payload": payload or {},
                    "artifacts": artifacts or {},
                    "quality": quality or {},
                },
                created_at=now,
            )
        return self.get_job(job_id)  # type: ignore[return-value]

    def update_job(self, job_id: str, status: str, **fields) -> dict:
        normalized_status = self._status(status)
        now = _now()
        allowed_direct = {
            "source_name",
            "source_path",
            "source_sha256",
            "final_name",
            "target_path",
            "metadata_json",
            "payload_json",
            "artifacts_json",
            "quality_json",
            "error",
        }
        values = {key: value for key, value in fields.items() if key in allowed_direct}
        json_inputs = {
            "metadata": "metadata_json",
            "payload": "payload_json",
            "artifacts": "artifacts_json",
            "quality": "quality_json",
        }
        for public_name, column in json_inputs.items():
            if public_name in fields:
                values[column] = self._json(fields[public_name])

        assignments = ["status = ?"]
        params: list[Any] = [normalized_status]
        for key, value in values.items():
            assignments.append(f"{key} = ?")
            params.append(value)
        assignments.extend(["revision = revision + 1", "updated_at = ?"])
        params.extend([now, job_id])

        with self._connect() as conn:
            cursor = conn.execute(
                f"UPDATE jobs SET {', '.join(assignments)} WHERE job_id = ?",
                params,
            )
            if cursor.rowcount != 1:
                raise KeyError(f"Unbekannter Job: {job_id}")
            self._insert_event(
                conn,
                job_id,
                "status",
                status=normalized_status,
                payload=fields,
                created_at=now,
            )
        return self.get_job(job_id)  # type: ignore[return-value]

    def get_job(self, job_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        return self._row_dict(row)

    def list_jobs(
        self,
        status: str | None = None,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        limit = self._bounded_limit(limit)
        offset = max(0, int(offset))
        with self._connect() as conn:
            if status is None:
                rows = conn.execute(
                    "SELECT * FROM jobs ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                    (limit, offset),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM jobs WHERE status = ? ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                    (self._status(status), limit, offset),
                ).fetchall()
        return [self._row_dict(row) for row in rows]  # type: ignore[misc]

    def list_resumable_jobs(self, limit: int = 100) -> list[dict]:
        placeholders = ",".join("?" for _ in RECOVERABLE_JOB_STATUSES)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM jobs WHERE status IN ({placeholders}) ORDER BY updated_at ASC LIMIT ?",
                (*RECOVERABLE_JOB_STATUSES, self._bounded_limit(limit)),
            ).fetchall()
        return [self._row_dict(row) for row in rows]  # type: ignore[misc]

    def resume_job(self, job_id: str, *, status: str = "resuming") -> dict:
        job = self.get_job(job_id)
        if job is None:
            raise KeyError(f"Unbekannter Job: {job_id}")
        if job["status"] in TERMINAL_JOB_STATUSES:
            raise ValueError(f"Abgeschlossener Job kann nicht wiederaufgenommen werden: {job_id}")
        normalized = self._status(status)
        updated = self.update_job(job_id, normalized)
        self.record_event(
            job_id,
            "resumed",
            status=normalized,
            payload={"previous_status": job["status"]},
        )
        return updated

    def record_event(
        self,
        job_id: str,
        event: str,
        *,
        stage: str = "",
        status: str = "",
        payload: dict | None = None,
    ) -> None:
        with self._connect() as conn:
            self._insert_event(
                conn,
                job_id,
                event,
                stage=stage,
                status=self._status(status) if status else "",
                payload=payload,
            )

    def _insert_event(
        self,
        conn: sqlite3.Connection,
        job_id: str,
        event: str,
        *,
        stage: str = "",
        status: str = "",
        payload: dict | None = None,
        created_at: str | None = None,
    ) -> None:
        conn.execute("""
            INSERT INTO job_events (job_id, event, stage, status, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (job_id, event, stage, status, self._json(payload), created_at or _now()))

    def list_job_events(self, job_id: str, limit: int = 500) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT * FROM job_events
                WHERE job_id = ?
                ORDER BY id ASC
                LIMIT ?
            """, (job_id, self._bounded_limit(limit, maximum=5000))).fetchall()
        return [self._row_dict(row) for row in rows]  # type: ignore[misc]

    # ------------------------------------------------------------------
    # Document index
    # ------------------------------------------------------------------

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
            """, (source_sha256, self._bounded_limit(limit))).fetchall()
        return [self._row_dict(row) for row in rows]  # type: ignore[misc]

    def index_document(
        self,
        *,
        source_sha256: str | None,
        source_name: str,
        final_name: str,
        target_path: str,
        outputs: dict,
        metadata: dict,
    ) -> int:
        now = _now()
        with self._connect() as conn:
            cursor = conn.execute("""
                INSERT INTO documents (
                    source_sha256, source_name, final_name, target_path,
                    outputs_json, metadata_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_sha256)
                WHERE source_sha256 IS NOT NULL AND TRIM(source_sha256) <> ''
                DO UPDATE SET
                    source_name = excluded.source_name,
                    final_name = excluded.final_name,
                    target_path = excluded.target_path,
                    outputs_json = excluded.outputs_json,
                    metadata_json = excluded.metadata_json,
                    updated_at = excluded.updated_at
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
            return int(cursor.lastrowid)

    def search_documents(self, query: str = "", limit: int = 100) -> list[dict]:
        pattern = f"%{query}%"
        with self._connect() as conn:
            if query:
                rows = conn.execute("""
                    SELECT * FROM documents
                    WHERE final_name LIKE ? OR source_name LIKE ? OR target_path LIKE ? OR metadata_json LIKE ?
                    ORDER BY updated_at DESC
                    LIMIT ?
                """, (pattern, pattern, pattern, pattern, self._bounded_limit(limit))).fetchall()
            else:
                rows = conn.execute("""
                    SELECT * FROM documents
                    ORDER BY updated_at DESC
                    LIMIT ?
                """, (self._bounded_limit(limit),)).fetchall()
        return [self._row_dict(row) for row in rows]  # type: ignore[misc]

    # ------------------------------------------------------------------
    # Persistent review and staging work
    # ------------------------------------------------------------------

    def add_review_item(
        self,
        *,
        job_id: str,
        kind: str,
        source_name: str,
        proposed_path: str,
        candidates: list[dict] | None = None,
        metadata: dict | None = None,
        payload: dict | None = None,
        artifacts: dict | list | None = None,
        quality: dict | None = None,
        status: str = "pending",
        error: str = "",
    ) -> int:
        normalized_status = self._status(status)
        now = _now()
        with self._connect() as conn:
            cursor = conn.execute("""
                INSERT INTO review_queue (
                    job_id, kind, status, source_name, proposed_path,
                    candidates_json, metadata_json, payload_json,
                    artifacts_json, quality_json, error,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                job_id,
                str(kind or "review").strip(),
                normalized_status,
                source_name,
                proposed_path,
                self._json(candidates or []),
                self._json(metadata),
                self._json(payload),
                self._json(artifacts),
                self._json(quality),
                error,
                now,
                now,
            ))
            item_id = int(cursor.lastrowid)
            self._sync_job_for_review(
                conn,
                job_id,
                normalized_status,
                event="review_queued",
                payload={"review_item_id": item_id, "kind": kind},
            )
            return item_id

    def add_staging_item(
        self,
        *,
        job_id: str,
        source_name: str,
        proposed_path: str,
        artifacts: dict | list,
        payload: dict | None = None,
        quality: dict | None = None,
        metadata: dict | None = None,
        candidates: list[dict] | None = None,
    ) -> int:
        """Create a restart-safe staging work item."""
        return self.add_review_item(
            job_id=job_id,
            kind="staging",
            source_name=source_name,
            proposed_path=proposed_path,
            candidates=candidates,
            metadata=metadata,
            payload=payload,
            artifacts=artifacts,
            quality=quality,
            status="staged",
        )

    # Convenient wording for callers that model staged work as jobs.
    create_staging_job = add_staging_item

    def get_review_item(self, item_id: int) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM review_queue WHERE id = ?", (int(item_id),)).fetchone()
        return self._row_dict(row)

    def get_review_by_job_id(
        self,
        job_id: str,
        *,
        recoverable_only: bool = True,
    ) -> dict | None:
        """Return the newest review row for one job without scanning a queue page.

        A direct indexed lookup is important for finalizing audit evidence: a
        busy archive may contain more rows than the bounded queue-list API can
        return, and the correctness of one job must never depend on backlog
        position.
        """
        normalized_job_id = str(job_id or "").strip()
        if not normalized_job_id:
            return None
        with self._connect() as conn:
            if recoverable_only:
                placeholders = ",".join("?" for _ in RECOVERABLE_REVIEW_STATUSES)
                row = conn.execute(
                    f"""
                    SELECT * FROM review_queue
                    WHERE job_id = ? AND status IN ({placeholders})
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (normalized_job_id, *RECOVERABLE_REVIEW_STATUSES),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT * FROM review_queue
                    WHERE job_id = ?
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (normalized_job_id,),
                ).fetchone()
        return self._row_dict(row)

    def claim_review_item(
        self,
        item_id: int,
        *,
        expected_revision: int,
        lease_seconds: int = DEFAULT_REVIEW_LEASE_SECONDS,
    ) -> dict | None:
        """Atomically claim recoverable review work for one resolver.

        The revision compare-and-swap prevents two GUI instances or a double
        click from publishing the same package concurrently.  The opaque token
        identifies the owner and the explicit expiry makes crash recovery
        independent of unrelated ``updated_at`` changes.
        """
        claimable = tuple(
            status for status in RECOVERABLE_REVIEW_STATUSES if status != "in_review"
        )
        placeholders = ",".join("?" for _ in claimable)
        now = _now()
        expires_at = (
            datetime.now(timezone.utc)
            + timedelta(seconds=max(60, int(lease_seconds)))
        ).isoformat()
        claim_token = uuid.uuid4().hex
        with self._connect() as conn:
            cursor = conn.execute(
                f"""
                UPDATE review_queue
                SET status = 'in_review', opened_at = COALESCE(opened_at, ?),
                    resolved_at = NULL, error = '', revision = revision + 1,
                    updated_at = ?, claim_token = ?, claim_expires_at = ?
                WHERE id = ? AND revision = ?
                  AND (
                    status IN ({placeholders})
                    OR (
                        status = 'in_review'
                        AND (claim_expires_at IS NULL OR claim_expires_at <= ?)
                    )
                  )
                """,
                (
                    now,
                    now,
                    claim_token,
                    expires_at,
                    int(item_id),
                    int(expected_revision),
                    *claimable,
                    now,
                ),
            )
            if cursor.rowcount != 1:
                return None
            row = conn.execute(
                "SELECT job_id FROM review_queue WHERE id = ?",
                (int(item_id),),
            ).fetchone()
            self._sync_job_for_review(
                conn,
                row["job_id"] if row else "",
                "in_review",
                event="review_claimed",
                payload={
                    "review_item_id": int(item_id),
                    "expected_revision": int(expected_revision),
                    "claim_expires_at": expires_at,
                },
            )
        return self.get_review_item(item_id)

    def update_review_item(
        self,
        item_id: int,
        *,
        status: str | None = None,
        claim_token: str | None = None,
        expected_revision: int | None = None,
        lease_seconds: int = DEFAULT_REVIEW_LEASE_SECONDS,
        **fields,
    ) -> dict:
        allowed_direct = {
            "kind",
            "source_name",
            "proposed_path",
            "chosen_path",
            "candidates_json",
            "metadata_json",
            "payload_json",
            "artifacts_json",
            "quality_json",
            "error",
        }
        values = {key: value for key, value in fields.items() if key in allowed_direct}
        json_inputs = {
            "candidates": "candidates_json",
            "metadata": "metadata_json",
            "payload": "payload_json",
            "artifacts": "artifacts_json",
            "quality": "quality_json",
        }
        for public_name, column in json_inputs.items():
            if public_name in fields:
                values[column] = self._json(fields[public_name])

        normalized_status = self._status(status) if status is not None else None
        now = _now()
        with self._connect() as conn:
            current = conn.execute(
                """
                SELECT job_id, status, claim_token, claim_expires_at, revision
                FROM review_queue WHERE id = ?
                """,
                (int(item_id),),
            ).fetchone()
            if current is None:
                raise KeyError(f"Unbekannter Review-Eintrag: {item_id}")
            current_revision = int(current["revision"] or 0)
            if expected_revision is not None and int(expected_revision) != current_revision:
                raise PermissionError(
                    "Der Review-Eintrag wurde zwischenzeitlich geändert; bitte neu laden."
                )
            active_token = str(current["claim_token"] or "")
            if active_token:
                if not claim_token or not hmac.compare_digest(active_token, str(claim_token)):
                    raise PermissionError(
                        "Der Review-Eintrag besitzt einen aktiven Claim eines anderen Bearbeiters."
                    )
                if str(current["claim_expires_at"] or "") <= now:
                    raise PermissionError("Der Review-Claim ist abgelaufen und muss neu übernommen werden.")

            assignments: list[str] = []
            params: list[Any] = []
            if normalized_status is not None:
                assignments.append("status = ?")
                params.append(normalized_status)
                if normalized_status in {"resolved", "dismissed"}:
                    assignments.append("resolved_at = ?")
                    params.append(now)
                else:
                    assignments.append("resolved_at = NULL")
            for key, value in values.items():
                assignments.append(f"{key} = ?")
                params.append(value)
            if normalized_status == "in_review" and active_token:
                expires_at = (
                    datetime.now(timezone.utc)
                    + timedelta(seconds=max(60, int(lease_seconds)))
                ).isoformat()
                assignments.append("claim_expires_at = ?")
                params.append(expires_at)
            elif normalized_status is not None and normalized_status != "in_review":
                assignments.extend(["claim_token = NULL", "claim_expires_at = NULL"])
            assignments.extend(["revision = revision + 1", "updated_at = ?"])
            params.extend(
                [
                    now,
                    int(item_id),
                    current_revision,
                    current["status"],
                    active_token,
                    active_token,
                    str(current["claim_expires_at"] or ""),
                ]
            )
            cursor = conn.execute(
                f"""
                UPDATE review_queue SET {', '.join(assignments)}
                WHERE id = ? AND revision = ? AND status = ?
                  AND ((claim_token IS NULL AND ? = '') OR claim_token = ?)
                  AND COALESCE(claim_expires_at, '') = ?
                """,
                params,
            )
            if cursor.rowcount != 1:
                raise PermissionError(
                    "Der Review-Eintrag oder sein Claim wurde gleichzeitig geändert; bitte neu laden."
                )

            effective_status = normalized_status or current["status"]
            self._sync_job_for_review(
                conn,
                current["job_id"],
                effective_status,
                event="review_updated",
                payload={"review_item_id": int(item_id), "fields": list(fields), "status": effective_status},
            )
        return self.get_review_item(item_id)  # type: ignore[return-value]

    def refresh_review_claim(
        self,
        item_id: int,
        claim_token: str,
        *,
        lease_seconds: int = DEFAULT_REVIEW_LEASE_SECONDS,
    ) -> dict:
        """Heartbeat a live claim without changing review content."""
        return self.update_review_item(
            item_id,
            status="in_review",
            claim_token=claim_token,
            lease_seconds=lease_seconds,
        )

    def finalize_review_transaction(
        self,
        item_id: int,
        *,
        claim_token: str,
        chosen_path: str,
        target_path: str,
        artifacts: dict,
        metadata: dict,
        payload: dict,
        quality: dict,
    ) -> dict:
        """Atomically resolve review, finish its job, and upsert the index.

        Repeating the call with the same claim token after a successful commit
        is harmless.  This supports callers that lose the return value after
        SQLite has durably committed.
        """
        chosen = str(chosen_path or "").strip().replace("\\", "/")
        if not chosen:
            raise ValueError("chosen_path darf nicht leer sein.")
        token = str(claim_token or "").strip()
        if not token:
            raise ValueError("claim_token darf nicht leer sein.")
        now = _now()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM review_queue WHERE id = ?",
                (int(item_id),),
            ).fetchone()
            if row is None:
                raise KeyError(f"Unbekannter Review-Eintrag: {item_id}")
            current_payload = self._decode_json(row["payload_json"], {})
            if (
                row["status"] == "resolved"
                and isinstance(current_payload, dict)
                and current_payload.get("resolution_transaction_id") == token
            ):
                return self._row_dict(row)  # type: ignore[return-value]
            if row["status"] != "in_review":
                raise PermissionError("Review-Eintrag ist nicht mehr aktiv beansprucht.")
            if not hmac.compare_digest(str(row["claim_token"] or ""), token):
                raise PermissionError("Review-Claim gehört einem anderen Bearbeiter.")
            if str(row["claim_expires_at"] or "") <= now:
                raise PermissionError("Review-Claim ist vor der Finalisierung abgelaufen.")

            final_payload = dict(payload or {})
            final_payload["resolution_transaction_id"] = token
            review_cursor = conn.execute(
                """
                UPDATE review_queue
                SET status = 'resolved', chosen_path = ?, metadata_json = ?,
                    payload_json = ?, artifacts_json = ?, quality_json = ?,
                    error = '', resolved_at = ?, updated_at = ?,
                    claim_token = NULL, claim_expires_at = NULL,
                    revision = revision + 1
                WHERE id = ? AND status = 'in_review' AND claim_token = ?
                """,
                (
                    chosen,
                    self._json(metadata),
                    self._json(final_payload),
                    self._json(artifacts),
                    self._json(quality),
                    now,
                    now,
                    int(item_id),
                    token,
                ),
            )
            if review_cursor.rowcount != 1:
                raise PermissionError("Review-Claim wurde während der Finalisierung ersetzt.")

            job_id = str(row["job_id"] or "")
            job = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            if job is not None:
                final_name = str(job["final_name"] or row["source_name"] or "")
                source_name = str(job["source_name"] or row["source_name"] or "")
                conn.execute(
                    """
                    UPDATE jobs
                    SET status = 'completed_after_review', final_name = ?,
                        target_path = ?, metadata_json = ?, artifacts_json = ?,
                        quality_json = ?, error = '', revision = revision + 1,
                        updated_at = ?
                    WHERE job_id = ?
                    """,
                    (
                        final_name,
                        target_path,
                        self._json(metadata),
                        self._json(artifacts),
                        self._json(quality),
                        now,
                        job_id,
                    ),
                )
                source_sha256 = str(job["source_sha256"] or "")
                if source_sha256:
                    conn.execute(
                        """
                        INSERT INTO documents (
                            source_sha256, source_name, final_name, target_path,
                            outputs_json, metadata_json, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(source_sha256)
                        WHERE source_sha256 IS NOT NULL AND TRIM(source_sha256) <> ''
                        DO UPDATE SET
                            source_name = excluded.source_name,
                            final_name = excluded.final_name,
                            target_path = excluded.target_path,
                            outputs_json = excluded.outputs_json,
                            metadata_json = excluded.metadata_json,
                            updated_at = excluded.updated_at
                        """,
                        (
                            source_sha256,
                            source_name,
                            final_name,
                            target_path,
                            self._json(artifacts),
                            self._json(metadata),
                            now,
                            now,
                        ),
                    )
                self._insert_event(
                    conn,
                    job_id,
                    "review_finalized",
                    status="completed_after_review",
                    payload={
                        "review_item_id": int(item_id),
                        "target_path": target_path,
                        "chosen_path": chosen,
                    },
                    created_at=now,
                )
            finalized = conn.execute(
                "SELECT * FROM review_queue WHERE id = ?",
                (int(item_id),),
            ).fetchone()
            return self._row_dict(finalized)  # type: ignore[return-value]

    def open_review_item(self, item_id: int) -> dict:
        current = self.get_review_item(item_id)
        if current is None:
            raise KeyError(f"Unbekannter Review-Eintrag: {item_id}")
        if current["status"] in {"resolved", "dismissed"}:
            raise ValueError("Ein abgeschlossener Review-Eintrag muss zuerst wiederaufgenommen werden.")
        now = _now()
        if current.get("claim_token") and str(current.get("claim_expires_at") or "") > now:
            raise PermissionError("Der Review-Eintrag wird bereits aktiv bearbeitet.")
        with self._connect() as conn:
            cursor = conn.execute("""
                UPDATE review_queue
                SET status = 'in_review', opened_at = ?, updated_at = ?, revision = revision + 1
                    , claim_token = NULL, claim_expires_at = NULL
                WHERE id = ? AND revision = ? AND status = ?
                  AND ((claim_token IS NULL AND ? = '') OR claim_token = ?)
                  AND COALESCE(claim_expires_at, '') = ?
            """, (
                now,
                now,
                int(item_id),
                int(current.get("revision") or 0),
                current["status"],
                str(current.get("claim_token") or ""),
                str(current.get("claim_token") or ""),
                str(current.get("claim_expires_at") or ""),
            ))
            if cursor.rowcount != 1:
                raise PermissionError(
                    "Der Review-Eintrag wurde gleichzeitig geändert; bitte neu laden."
                )
            self._sync_job_for_review(
                conn,
                current.get("job_id", ""),
                "in_review",
                event="review_opened",
                payload={"review_item_id": int(item_id)},
            )
        return self.get_review_item(item_id)  # type: ignore[return-value]

    def resolve_review_item(
        self,
        item_id: int | None,
        chosen_path: str,
        **fields,
    ) -> dict | None:
        if not item_id:
            return None
        chosen = str(chosen_path or "").strip().replace("\\", "/")
        if not chosen:
            raise ValueError("chosen_path darf nicht leer sein.")
        return self.update_review_item(
            int(item_id),
            status="resolved",
            chosen_path=chosen,
            **fields,
        )

    def dismiss_review_item(self, item_id: int, *, reason: str = "") -> dict:
        return self.update_review_item(item_id, status="dismissed", error=reason)

    def resume_review_item(self, item_id: int, *, status: str = "pending") -> dict:
        normalized_status = self._status(status)
        if normalized_status in {"resolved", "dismissed"}:
            raise ValueError("Wiederaufnahme benötigt einen offenen Status.")
        now = _now()
        with self._connect() as conn:
            current = conn.execute(
                """
                SELECT job_id, status, claim_token, claim_expires_at, revision
                FROM review_queue WHERE id = ?
                """,
                (int(item_id),),
            ).fetchone()
            if current is None:
                raise KeyError(f"Unbekannter Review-Eintrag: {item_id}")
            if current["claim_token"] and str(current["claim_expires_at"] or "") > now:
                raise PermissionError("Ein aktiver Review-Claim muss vor der Wiederaufnahme ablaufen.")
            cursor = conn.execute("""
                UPDATE review_queue
                SET status = ?, resolved_at = NULL, error = '',
                    resume_count = resume_count + 1,
                    revision = revision + 1, updated_at = ?,
                    claim_token = NULL, claim_expires_at = NULL
                WHERE id = ? AND revision = ? AND status = ?
                  AND ((claim_token IS NULL AND ? = '') OR claim_token = ?)
                  AND COALESCE(claim_expires_at, '') = ?
            """, (
                normalized_status,
                now,
                int(item_id),
                int(current["revision"] or 0),
                current["status"],
                str(current["claim_token"] or ""),
                str(current["claim_token"] or ""),
                str(current["claim_expires_at"] or ""),
            ))
            if cursor.rowcount != 1:
                raise PermissionError(
                    "Der Review-Eintrag oder sein Claim wurde gleichzeitig geändert; bitte neu laden."
                )
            self._sync_job_for_review(
                conn,
                current["job_id"],
                normalized_status,
                event="review_resumed",
                payload={
                    "review_item_id": int(item_id),
                    "previous_status": current["status"],
                },
            )
        return self.get_review_item(item_id)  # type: ignore[return-value]

    # Familiar alias for UI wording.
    reopen_review_item = resume_review_item

    def delete_review_item(self, item_id: int) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT job_id, status FROM review_queue WHERE id = ?",
                (int(item_id),),
            ).fetchone()
            if row is None:
                return False
            if row["status"] not in {"resolved", "dismissed"}:
                raise ValueError("Nur abgeschlossene Review-Einträge dürfen gelöscht werden.")
            conn.execute("DELETE FROM review_queue WHERE id = ?", (int(item_id),))
            self._insert_event(
                conn,
                row["job_id"],
                "review_deleted",
                payload={"review_item_id": int(item_id)},
            )
            return True

    def list_review_items(
        self,
        status: str | None = "pending",
        limit: int = 100,
        *,
        offset: int = 0,
    ) -> list[dict]:
        limit = self._bounded_limit(limit)
        offset = max(0, int(offset))
        with self._connect() as conn:
            if status is None:
                rows = conn.execute("""
                    SELECT * FROM review_queue
                    ORDER BY updated_at DESC, id DESC
                    LIMIT ? OFFSET ?
                """, (limit, offset)).fetchall()
            else:
                rows = conn.execute("""
                    SELECT * FROM review_queue
                    WHERE status = ?
                    ORDER BY updated_at DESC, id DESC
                    LIMIT ? OFFSET ?
                """, (self._status(status), limit, offset)).fetchall()
        return [self._row_dict(row) for row in rows]  # type: ignore[misc]

    def list_recoverable_review_items(self, limit: int = 100) -> list[dict]:
        placeholders = ",".join("?" for _ in RECOVERABLE_REVIEW_STATUSES)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                    SELECT * FROM review_queue
                    WHERE status IN ({placeholders})
                    ORDER BY updated_at ASC, id ASC
                    LIMIT ?
                """,
                (*RECOVERABLE_REVIEW_STATUSES, self._bounded_limit(limit)),
            ).fetchall()
        return [self._row_dict(row) for row in rows]  # type: ignore[misc]

    # Alias for callers interested in all recoverable review/staging work.
    list_recoverable_work = list_recoverable_review_items

    def _sync_job_for_review(
        self,
        conn: sqlite3.Connection,
        job_id: str,
        review_status: str,
        *,
        event: str,
        payload: dict,
    ) -> None:
        if not job_id:
            return
        row = conn.execute("SELECT status FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        if row is None or row["status"] in TERMINAL_JOB_STATUSES:
            return
        if review_status == "staged":
            job_status = "staged"
        elif review_status in {"resolved", "dismissed", "ready_to_resume"}:
            job_status = "ready_to_resume"
        else:
            job_status = "review_required"
        now = _now()
        conn.execute("""
            UPDATE jobs
            SET status = ?, revision = revision + 1, updated_at = ?
            WHERE job_id = ?
        """, (job_status, now, job_id))
        self._insert_event(
            conn,
            job_id,
            event,
            status=job_status,
            payload=payload,
            created_at=now,
        )
