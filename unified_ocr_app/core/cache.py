import sqlite3
import hashlib
import logging
from pathlib import Path

logger = logging.getLogger("UnifiedOCR")

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
            conn.close()
            logger.info(f"SQLite-Cache initialisiert unter: {self.db_path}")
        except Exception as e:
            logger.error(f"Fehler bei der Initialisierung der SQLite-Datenbank: {e}")

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
