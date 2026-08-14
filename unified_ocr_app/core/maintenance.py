from __future__ import annotations

import os
import shutil
import stat
import sys
import time
from pathlib import Path
from typing import Any

from core.config import AppConfig


def remove_directory_tree(directory: Path, *, attempts: int = 3, delay: float = 0.3) -> bool:
    """
    Remove a directory tree and tolerate short-lived Windows file locks.

    Virus scanners and the search indexer routinely hold a freshly written file
    open for a moment.  A single ``shutil.rmtree`` therefore fails sporadically
    and leaves an orphaned work directory behind.  Read-only flags are cleared
    and the removal is retried a few times before giving up.
    """
    directory = Path(directory)
    if not directory.exists():
        return True

    def _clear_readonly(func, path, _error):
        try:
            os.chmod(path, stat.S_IWRITE)
        except OSError:
            return
        func(path)

    # onerror ist ab Python 3.12 deprecated und wird durch onexc ersetzt.
    if sys.version_info >= (3, 12):
        handler = {"onexc": _clear_readonly}
    else:
        handler = {"onerror": _clear_readonly}

    last_error: Exception | None = None
    for attempt in range(max(1, attempts)):
        try:
            shutil.rmtree(directory, **handler)
            return True
        except Exception as exc:  # pragma: no cover - platform dependent
            last_error = exc
            if attempt < attempts - 1:
                time.sleep(delay)
    if last_error is not None and directory.exists():
        return False
    return not directory.exists()


def cleanup_runtime_artifacts(
    config: AppConfig,
    *,
    include_work: bool = True,
    include_error: bool = False,
    include_logs: bool = False,
    include_legacy_temp_work: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """
    Remove selected runtime artifacts from reserved app folders.

    This function deliberately refuses to touch original/ and final/. It keeps
    the reserved directories themselves in place and returns an audit summary.

    ``include_legacy_temp_work`` covers ``<base>/temp_work``.  Older versions
    rendered page images there; the current pipeline no longer writes to it, so
    nothing ever cleaned it up and rendered pages of real documents kept piling
    up outside the archive.
    """
    targets = []
    if include_work:
        targets.append(("work", Path(config.work_dir)))
    if include_error:
        targets.append(("error", Path(config.error_dir)))
    if include_logs:
        targets.append(("logs", Path(config.log_dir)))
    if include_legacy_temp_work:
        targets.append(("temp_work", Path(config.base_dir) / "temp_work"))

    audit = {
        "dry_run": bool(dry_run),
        "deleted": [],
        "failed": [],
        "skipped": [],
    }
    for label, directory in targets:
        safe_dir = _reserved_child(config.base_dir, directory, label)
        if not safe_dir.exists():
            audit["skipped"].append({"label": label, "path": str(safe_dir), "reason": "missing"})
            continue
        for item in safe_dir.iterdir():
            entry = {"label": label, "path": str(item)}
            try:
                if dry_run:
                    audit["deleted"].append({**entry, "dry_run": True})
                elif item.is_dir():
                    if not remove_directory_tree(item):
                        raise OSError("Verzeichnis konnte nicht vollstaendig entfernt werden.")
                    audit["deleted"].append(entry)
                else:
                    item.unlink()
                    audit["deleted"].append(entry)
            except Exception as exc:
                audit["failed"].append({**entry, "error": str(exc)})
    return audit


def _reserved_child(base_dir: Path, directory: Path, label: str) -> Path:
    base = Path(base_dir).resolve(strict=False)
    candidate = Path(directory).resolve(strict=False)
    allowed = {
        "work": base / "work",
        "error": base / "error",
        "logs": base / "logs",
        # Altbestand frueherer Versionen, wird nicht mehr beschrieben.
        "temp_work": base / "temp_work",
    }
    expected = allowed.get(label)
    if expected is None:
        raise ValueError(f"Unbekannter Cleanup-Bereich: {label}")
    if candidate != expected.resolve(strict=False):
        raise ValueError(f"Unsicherer Cleanup-Zielpfad fuer {label}: {candidate}")
    return Path(directory)
