"""Privacy-conscious per-job diagnostics for debugging and tuning."""

from __future__ import annotations

import json
import platform
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.cache import sha256_file, sha256_text


SECRET_KEYWORDS = ("key", "token", "password", "secret", "credential")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _path_value(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _path_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_path_value(v) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            key_str = str(key)
            if any(marker in key_str.lower() for marker in SECRET_KEYWORDS):
                cleaned[key_str] = "<redacted>"
            else:
                cleaned[key_str] = _sanitize(item)
        return cleaned
    if isinstance(value, (list, tuple)):
        return [_sanitize(item) for item in value]
    return _path_value(value)


def text_stats(text: str | None, *, preview_chars: int = 120) -> dict[str, Any]:
    value = "" if text is None else str(text)
    stripped = value.strip()
    lines = [line for line in value.splitlines() if line.strip()]
    words = stripped.split()
    return {
        "chars": len(value),
        "non_ws_chars": sum(1 for char in value if not char.isspace()),
        "lines": len(lines),
        "words": len(words),
        "sha256": sha256_text(value) if value else "",
        "preview": stripped[:preview_chars],
    }


def page_text_stats(pages: dict[int, str] | None, *, preview_chars: int = 80) -> dict[str, Any]:
    result = {}
    for page_num, text in sorted((pages or {}).items()):
        result[str(page_num)] = text_stats(text, preview_chars=preview_chars)
    return result


def artifact_stats(outputs: dict[str, Any] | None) -> dict[str, Any]:
    result = {}
    for key, value in (outputs or {}).items():
        if not value:
            result[key] = None
            continue
        path = Path(value)
        result[key] = {
            "path": str(path),
            "exists": path.exists(),
            "size_bytes": path.stat().st_size if path.exists() else None,
            "sha256": sha256_file(path) if path.exists() and path.is_file() else None,
        }
    return result


class DiagnosticsRecorder:
    """Collects compact debugging information without storing full document text."""

    def __init__(self, *, job_id: str, source_path: Path, enabled: bool = True):
        self.enabled = bool(enabled)
        self.started_at = time.perf_counter()
        self.data: dict[str, Any] = {
            "schema": "unified_ocr_diagnostics_v1",
            "job_id": job_id,
            "created_at": _now_iso(),
            "source": {
                "name": Path(source_path).name,
                "suffix": Path(source_path).suffix.lower(),
                "path": str(source_path),
                "sha256": sha256_file(source_path),
            },
            "runtime": {
                "python": platform.python_version(),
                "platform": platform.platform(),
            },
            "config": {},
            "events": [],
            "stages": {},
            "text_sources": {},
            "layout": {},
            "outputs": {},
            "sync": {},
            "warnings": [],
        }

    def configure(self, **config):
        if not self.enabled:
            return
        self.data["config"].update(_sanitize(config))

    def event(self, name: str, **payload):
        if not self.enabled:
            return
        self.data["events"].append({
            "time_utc": _now_iso(),
            "name": name,
            "payload": _sanitize(payload),
        })

    def stage(self, name: str, *, status: str = "ok", start: float | None = None, **payload):
        if not self.enabled:
            return
        entry = {
            "status": status,
            "time_utc": _now_iso(),
            "payload": _sanitize(payload),
        }
        if start is not None:
            entry["duration_ms"] = round((time.perf_counter() - start) * 1000, 2)
        self.data["stages"][name] = entry

    def record_text_sources(self, **sources):
        if not self.enabled:
            return
        for key, value in sources.items():
            if isinstance(value, dict):
                self.data["text_sources"][key] = page_text_stats(value)
            else:
                self.data["text_sources"][key] = text_stats(value)

    def record_layout(self, layout_summary: dict | None):
        if self.enabled:
            self.data["layout"] = _sanitize(layout_summary or {})

    def record_outputs(self, outputs: dict[str, Any] | None):
        if self.enabled:
            self.data["outputs"] = artifact_stats(outputs)

    def record_sync(self, **sync_data):
        if self.enabled:
            self.data["sync"].update(_sanitize(sync_data))

    def warn(self, message: str, **payload):
        if not self.enabled:
            return
        self.data["warnings"].append({
            "time_utc": _now_iso(),
            "message": message,
            "payload": _sanitize(payload),
        })

    def write_copy(self, destination: Path) -> Path | None:
        if not self.enabled:
            return None
        self.data["finished_at"] = _now_iso()
        self.data["duration_ms"] = round((time.perf_counter() - self.started_at) * 1000, 2)
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8")
        return destination
