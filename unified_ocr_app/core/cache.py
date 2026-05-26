import sqlite3
import hashlib
import logging
import json
from dataclasses import dataclass, field
from typing import Any
from pathlib import Path

logger = logging.getLogger("UnifiedOCR")


def sha256_text(value: str | None) -> str:
    """Return a stable SHA-256 hex digest for text cache/audit inputs."""
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()


def sha256_file(path: str | Path) -> str | None:
    """Return a SHA-256 hex digest for a file, or None if it cannot be read."""
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return None


@dataclass(frozen=True)
class CacheInput:
    task: str
    model: str = ""
    prompt_version: str = ""
    system_prompt_hash: str = ""
    user_prompt_hash: str = ""
    image_sha256: str | None = None
    source_hashes: dict[str, str] = field(default_factory=dict)
    options: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": "llm_cache_v2",
            "task": self.task,
            "model": self.model,
            "prompt_version": self.prompt_version,
            "system_prompt_hash": self.system_prompt_hash,
            "user_prompt_hash": self.user_prompt_hash,
            "image_sha256": self.image_sha256,
            "source_hashes": dict(sorted(self.source_hashes.items())),
            "options": dict(sorted(self.options.items())),
        }


def build_cache_key(cache_input: CacheInput | dict[str, Any]) -> str:
    """Build a deterministic SHA-256 cache key from structured task input."""
    payload = cache_input.to_payload() if isinstance(cache_input, CacheInput) else dict(cache_input)
    payload.setdefault("schema", "llm_cache_v2")
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return "v2:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()

class SQLiteCache:
    """
    Verwaltet das lokale Caching von LLM-Ergebnissen mit einer SQLite-Datenbank.
    Erzeugt einen eindeutigen Schlüssel aus dem MD5-Hash des rohen Textinhalts,
    dem Namen des LLM-Modells und der Version des Prompts.
    """
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.conn = None
        self._init_db()

    def _get_connection(self):
        """Erzeugt eine threadsichere Verbindung zur SQLite-Datenbank mit Timeout."""
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        """Initialisiert die Tabellenstruktur, falls noch nicht vorhanden."""
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = self._get_connection()
            with conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS llm_cache (
                        hash_key TEXT PRIMARY KEY,
                        result_text TEXT,
                        raw_text_md5 TEXT,
                        model_name TEXT,
                        prompt_version TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                self._ensure_column(conn, "llm_cache", "raw_text_sha256", "TEXT")
                self._ensure_column(conn, "llm_cache", "key_schema", "TEXT")
            conn.close()
            logger.info(f"SQLite-Cache initialisiert unter: {self.db_path}")
        except Exception as e:
            logger.error(f"Fehler bei der Initialisierung der SQLite-Datenbank: {e}")

    def _ensure_column(self, conn, table: str, column: str, column_type: str):
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")

    def get(self, raw_text: str, model_name: str, prompt_version: str) -> str | None:
        """
        Sucht nach einem passenden Eintrag im Cache.
        Gibt das Ergebnis zurück oder None bei einem Cache-Miss.
        """
        if not raw_text:
            return None
        try:
            # MD5-Hash des rohen Texts berechnen
            raw_text_md5 = hashlib.md5(raw_text.encode("utf-8")).hexdigest()
            # Eindeutigen Kombinationsschlüssel erstellen
            combined = f"{raw_text_md5}:{model_name}:{prompt_version}"
            hash_key = hashlib.md5(combined.encode("utf-8")).hexdigest()
            
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT result_text FROM llm_cache WHERE hash_key = ?", (hash_key,))
            row = cursor.fetchone()
            conn.close()
            
            if row:
                logger.info(f"SQLite-Cache HIT für Modell '{model_name}' (Key: {hash_key})")
                return row["result_text"]
            
            logger.info(f"SQLite-Cache MISS für Modell '{model_name}' (Key: {hash_key})")
            return None
        except Exception as e:
            logger.error(f"Fehler beim Lesen aus dem Cache: {e}")
            return None

    def get_by_key(self, hash_key: str, model_name: str = "") -> str | None:
        """Sucht einen strukturierten Cache-Eintrag per bereits berechnetem Key."""
        if not hash_key:
            return None
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT result_text FROM llm_cache WHERE hash_key = ?", (hash_key,))
            row = cursor.fetchone()
            conn.close()

            if row:
                logger.info(f"SQLite-Cache HIT fÃ¼r Modell '{model_name}' (Key: {hash_key})")
                return row["result_text"]

            logger.info(f"SQLite-Cache MISS fÃ¼r Modell '{model_name}' (Key: {hash_key})")
            return None
        except Exception as e:
            logger.error(f"Fehler beim Lesen aus dem Cache: {e}")
            return None

    def set(self, raw_text: str, model_name: str, prompt_version: str, result_text: str):
        """Speichert ein neues LLM-Ergebnis im Cache (überschreibt bestehende Einträge)."""
        if not raw_text or not result_text:
            return
        try:
            raw_text_md5 = hashlib.md5(raw_text.encode("utf-8")).hexdigest()
            combined = f"{raw_text_md5}:{model_name}:{prompt_version}"
            hash_key = hashlib.md5(combined.encode("utf-8")).hexdigest()
            
            conn = self._get_connection()
            with conn:
                conn.execute("""
                    INSERT OR REPLACE INTO llm_cache (hash_key, result_text, raw_text_md5, model_name, prompt_version)
                    VALUES (?, ?, ?, ?, ?)
                """, (hash_key, result_text, raw_text_md5, model_name, prompt_version))
            conn.close()
            logger.info(f"SQLite-Cache Eintrag gespeichert für Modell '{model_name}' (Key: {hash_key})")
        except Exception as e:
            logger.error(f"Fehler beim Schreiben in den Cache: {e}")

    def set_by_key(
        self,
        hash_key: str,
        model_name: str,
        prompt_version: str,
        result_text: str,
        *,
        raw_text: str = "",
        key_schema: str = "llm_cache_v2",
    ):
        """Speichert ein Ergebnis unter einem strukturierten SHA-256 Cache-Key."""
        if not hash_key or not result_text:
            return
        try:
            raw_text_sha256 = sha256_text(raw_text)
            conn = self._get_connection()
            with conn:
                conn.execute("""
                    INSERT OR REPLACE INTO llm_cache
                        (hash_key, result_text, raw_text_md5, model_name, prompt_version, raw_text_sha256, key_schema)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (hash_key, result_text, "", model_name, prompt_version, raw_text_sha256, key_schema))
            conn.close()
            logger.info(f"SQLite-Cache Eintrag gespeichert fÃ¼r Modell '{model_name}' (Key: {hash_key})")
        except Exception as e:
            logger.error(f"Fehler beim Schreiben in den Cache: {e}")
