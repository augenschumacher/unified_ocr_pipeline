"""Runtime paths and small filesystem hardening helpers.

The application directory should stay publishable: no OAuth tokens, no local
machine settings, no generated logs. User-specific runtime data belongs in the
operating system's app-data directory.
"""

from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path


APP_NAME = "UnifiedOCR"


def get_user_data_dir() -> Path:
    """Return the per-user data directory for settings, tokens, and state."""
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA")
        if base:
            return Path(base) / APP_NAME
    return Path.home() / ".unified_ocr"


def default_settings_path() -> Path:
    return get_user_data_dir() / "settings.json"


def default_token_path() -> Path:
    return get_user_data_dir() / "google_drive_token.json"


def default_credentials_path() -> Path:
    return get_user_data_dir() / "credentials.json"


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def legacy_settings_path() -> Path:
    return project_root() / "settings.json"


def legacy_token_path() -> Path:
    return project_root() / "token.json"


def legacy_credentials_path() -> Path:
    return project_root() / "credentials.json"


def harden_private_file(path: Path) -> None:
    """Best-effort owner-only permission hardening for sensitive files."""
    try:
        path = Path(path)
        if not path.exists():
            return
        path.chmod(stat.S_IREAD | stat.S_IWRITE)
    except OSError:
        # Windows ACLs and corporate policies can reject chmod; app-data
        # placement is still the primary protection.
        return


def copy_legacy_file_if_missing(legacy: Path, target: Path) -> bool:
    """Copy a legacy app-local file to the user-data directory if needed."""
    legacy = Path(legacy)
    target = Path(target)
    try:
        if not legacy.exists() or target.exists():
            return False
        if legacy.resolve() == target.resolve():
            return False
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(legacy, target)
        harden_private_file(target)
        try:
            legacy.unlink()
        except OSError:
            pass
        return True
    except OSError:
        return False


def normalize_token_path(value: str | os.PathLike | None) -> str:
    """Map unsafe legacy token locations to the per-user app-data path."""
    if not value:
        return str(default_token_path())

    raw = str(value).strip()
    if raw.lower() == "token.json":
        copy_legacy_file_if_missing(legacy_token_path(), default_token_path())
        return str(default_token_path())

    path = Path(raw)
    if not path.is_absolute() and path.name.lower() == "token.json":
        copy_legacy_file_if_missing(legacy_token_path(), default_token_path())
        return str(default_token_path())

    return raw


def normalize_credentials_path(value: str | os.PathLike | None) -> str:
    """Map unsafe legacy credentials locations to the per-user app-data path."""
    if not value:
        return str(default_credentials_path())

    raw = str(value).strip()
    if raw.lower() == "credentials.json":
        copy_legacy_file_if_missing(legacy_credentials_path(), default_credentials_path())
        return str(default_credentials_path())

    path = Path(raw)
    if not path.is_absolute() and path.name.lower() == "credentials.json":
        copy_legacy_file_if_missing(legacy_credentials_path(), default_credentials_path())
        return str(default_credentials_path())

    return raw

