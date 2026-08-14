"""Structured workflow status events and verified upload summaries.

The GUI must never infer completion from a percentage or a translated log
message.  This module provides a small, dependency-free contract shared by the
pipeline, the desktop UI and tests.
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Any, Mapping


WORKFLOW_EVENT_SCHEMA = "unified_ocr_workflow_status_v1"

WORKFLOW_STEPS = (
    "input",
    "ocr",
    "quality",
    "metadata",
    "export",
    "archive",
    "google_drive",
    "synology",
    "complete",
)

WORKFLOW_STEP_LABELS = {
    "input": "Original gesichert",
    "ocr": "OCR / Texterfassung",
    "quality": "Qualitätsprüfung",
    "metadata": "Metadaten & Tags",
    "export": "Export",
    "archive": "Ablage",
    "google_drive": "Google Drive",
    "synology": "Synology / NAS",
    "complete": "Abschluss",
}

WORKFLOW_STATES = frozenset(
    {"pending", "running", "success", "warning", "error", "skipped"}
)
WORKFLOW_TERMINAL_STATES = frozenset(
    {"success", "warning", "error", "skipped"}
)

WORKFLOW_STATE_STYLES = {
    "pending": {
        "color": "#6B7280",
        "hover": "#4B5563",
        "symbol": "○",
        "label": "Ausstehend",
    },
    "running": {
        "color": "#2563EB",
        "hover": "#1D4ED8",
        "symbol": "…",
        "label": "Läuft",
    },
    "success": {
        "color": "#16A34A",
        "hover": "#15803D",
        "symbol": "✓",
        "label": "Erledigt",
    },
    "warning": {
        "color": "#D97706",
        "hover": "#B45309",
        "symbol": "!",
        "label": "Prüfen",
    },
    "error": {
        "color": "#DC2626",
        "hover": "#B91C1C",
        "symbol": "×",
        "label": "Fehler",
    },
    "skipped": {
        "color": "#6B7280",
        "hover": "#4B5563",
        "symbol": "–",
        "label": "Nicht aktiv",
    },
}


def make_workflow_event(
    step: str,
    state: str,
    message: str = "",
    *,
    job_id: str = "",
    source_name: str = "",
    details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return one validated, JSON-serialisable workflow event.

    ``step='job'`` is reserved for resetting the UI at the start of a new
    document.  It is intentionally not rendered as a button.
    """
    step = str(step or "").strip().lower()
    state = str(state or "").strip().lower()
    if step != "job" and step not in WORKFLOW_STEPS:
        raise ValueError(f"Unbekannter Workflow-Schritt: {step!r}")
    if state not in WORKFLOW_STATES:
        raise ValueError(f"Unbekannter Workflow-Status: {state!r}")
    return {
        "schema": WORKFLOW_EVENT_SCHEMA,
        "emitted_at_epoch": time.time(),
        "job_id": str(job_id or ""),
        "source_name": str(source_name or ""),
        "step": step,
        "state": state,
        "message": str(message or "").strip(),
        "details": dict(details or {}),
    }


def workflow_button_view(step: str, state: str, details: Mapping[str, Any] | None = None) -> dict[str, str]:
    """Return accessible text and colours for one status button."""
    if step not in WORKFLOW_STEP_LABELS:
        raise ValueError(f"Unbekannter Workflow-Schritt: {step!r}")
    style = WORKFLOW_STATE_STYLES.get(state, WORKFLOW_STATE_STYLES["pending"])
    state_label = style["label"]
    details = details if isinstance(details, Mapping) else {}
    if state == "success" and step in {"google_drive", "synology"}:
        confirmed = int(details.get("confirmed_count") or 0)
        expected = int(details.get("expected_count") or confirmed)
        if confirmed and expected:
            state_label = f"{confirmed}/{expected} bestätigt"
    elif state == "success" and step == "input":
        state_label = "Gesichert"
    elif state == "success" and step == "quality":
        state_label = "Bestanden"
    elif state == "success" and step == "archive":
        state_label = "Abgelegt"
    elif state == "success" and step == "complete":
        state_label = "Abgeschlossen"
    return {
        "text": f"{WORKFLOW_STEP_LABELS[step]}\n{style['symbol']} {state_label}",
        "color": style["color"],
        "hover": style["hover"],
    }


def _md5(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_expected_drive_members(items: Mapping[str, str | Path]) -> dict[str, dict[str, Any]]:
    """Capture the local members that must be confirmed by a Drive audit."""
    expected: dict[str, dict[str, Any]] = {}
    for raw_role, raw_path in (items or {}).items():
        role = str(raw_role or "").strip()
        path = Path(raw_path)
        if not role or not path.is_file():
            continue
        expected[role] = {
            "path": str(path),
            "filename": path.name,
            "content_md5": _md5(path),
            "content_size": path.stat().st_size,
        }
    return expected


def summarize_google_drive_audit(
    entries: Any,
    expected_members: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Validate a complete Drive package before the UI may turn green.

    ``created`` and ``created_conflict`` require the client's post-create
    verification flag.  ``duplicate`` is also a positive presence
    confirmation because the Drive client only emits it after matching content
    digest and size.  Every expected role must occur exactly once.
    """
    expected = dict(expected_members or {})
    base_details: dict[str, Any] = {
        "expected_count": len(expected),
        "confirmed_count": 0,
        "uploaded_count": 0,
        "duplicate_count": 0,
        "conflict_count": 0,
        "remote_files": [],
        "validation_errors": [],
    }
    if not expected:
        return {
            "state": "skipped",
            "message": "Für Google Drive wurden keine vorhandenen Ausgabedateien ausgewählt.",
            "details": base_details,
        }
    if not isinstance(entries, list):
        base_details["validation_errors"].append("Drive-Audit ist keine Liste.")
        return {
            "state": "error",
            "message": "Google Drive konnte den Upload nicht vollständig bestätigen.",
            "details": base_details,
        }

    audits = [entry for entry in entries if isinstance(entry, Mapping)]
    errors: list[str] = []
    if len(entries) != len(expected):
        errors.append(
            f"Erwartet wurden {len(expected)} Paketmitglieder, geliefert wurden {len(entries)} Audit-Einträge."
        )
    if len(audits) != len(entries):
        errors.append("Drive-Audit enthält mindestens einen ungültigen Eintrag.")

    by_role: dict[str, Mapping[str, Any]] = {}
    for entry in audits:
        role = str(entry.get("role") or "").strip()
        if not role or role in by_role:
            errors.append("Drive-Audit enthält eine fehlende oder doppelte Rolle.")
            continue
        by_role[role] = entry

    common_values = {"package_id": set(), "package_stem": set(), "drive_folder_id": set()}
    remote_files: list[dict[str, Any]] = []
    uploaded_count = duplicate_count = conflict_count = 0
    allowed_actions = {"created", "created_conflict", "duplicate"}

    for role, local in expected.items():
        entry = by_role.get(role)
        if entry is None:
            errors.append(f"Paketrolle {role!r} wurde nicht bestätigt.")
            continue
        action = str(entry.get("action") or "").strip().lower()
        if entry.get("provider") != "google_drive":
            errors.append(f"Paketrolle {role!r} hat einen falschen Provider.")
        if action not in allowed_actions:
            errors.append(f"Paketrolle {role!r} hat die unbekannte Aktion {action!r}.")
        if not str(entry.get("drive_file_id") or "").strip():
            errors.append(f"Paketrolle {role!r} besitzt keine Drive-Datei-ID.")
        if not str(entry.get("drive_folder_id") or "").strip():
            errors.append(f"Paketrolle {role!r} besitzt keine Drive-Ordner-ID.")
        if str(entry.get("content_md5") or "").lower() != str(local.get("content_md5") or "").lower():
            errors.append(f"Paketrolle {role!r} hat keinen passenden MD5-Nachweis.")
        try:
            remote_size = int(entry.get("content_size"))
        except (TypeError, ValueError):
            remote_size = -1
        if remote_size != int(local.get("content_size") or 0):
            errors.append(f"Paketrolle {role!r} hat keinen passenden Größen-Nachweis.")
        if action in {"created", "created_conflict"} and entry.get("post_create_verified") is not True:
            errors.append(f"Paketrolle {role!r} wurde nach dem Upload nicht bestätigt.")

        for key in common_values:
            value = str(entry.get(key) or "").strip()
            if not value:
                errors.append(f"Paketrolle {role!r} enthält kein {key}.")
            else:
                common_values[key].add(value)

        if action == "duplicate":
            duplicate_count += 1
        elif action in {"created", "created_conflict"}:
            uploaded_count += 1
        if action == "created_conflict":
            conflict_count += 1
        remote_files.append(
            {
                "role": role,
                "filename": str(entry.get("remote_filename") or entry.get("filename") or local.get("filename") or ""),
                "drive_file_id": str(entry.get("drive_file_id") or ""),
                "action": action,
                "folder_path": str(entry.get("folder_path") or ""),
            }
        )

    unexpected_roles = sorted(set(by_role) - set(expected))
    if unexpected_roles:
        errors.append("Unerwartete Drive-Paketrollen: " + ", ".join(unexpected_roles))
    for key, values in common_values.items():
        if len(values) > 1:
            errors.append(f"Drive-Audit enthält widersprüchliche Werte für {key}.")

    base_details.update(
        {
            "uploaded_count": uploaded_count,
            "duplicate_count": duplicate_count,
            "conflict_count": conflict_count,
            "remote_files": remote_files,
            "validation_errors": list(dict.fromkeys(errors)),
        }
    )
    if errors:
        return {
            "state": "error",
            "message": "Google Drive konnte nicht alle vorgesehenen Dateien sicher bestätigen.",
            "details": base_details,
        }

    confirmed_count = len(expected)
    base_details["confirmed_count"] = confirmed_count
    if uploaded_count and duplicate_count:
        message = (
            f"Google Drive bestätigt: {uploaded_count} neu hochgeladen, "
            f"{duplicate_count} bereits identisch vorhanden."
        )
    elif uploaded_count:
        message = f"Google Drive bestätigt: {uploaded_count} Datei(en) neu hochgeladen und geprüft."
    else:
        message = f"Google Drive bestätigt: {duplicate_count} Datei(en) bereits identisch vorhanden."
    if conflict_count:
        message += f" {conflict_count} Datei(en) erhielten einen sicheren Konfliktnamen."
    return {"state": "success", "message": message, "details": base_details}


def summarize_synology_audit(
    entries: Any,
    expected_count: int,
) -> dict[str, Any]:
    """Return a conservative status summary for Synology/WebDAV uploads."""
    details = {
        "expected_count": max(0, int(expected_count or 0)),
        "confirmed_count": 0,
        "remote_files": [],
    }
    if not expected_count:
        return {
            "state": "skipped",
            "message": "Für Synology/NAS wurden keine vorhandenen Ausgabedateien ausgewählt.",
            "details": details,
        }
    if not isinstance(entries, list) or len(entries) != expected_count:
        return {
            "state": "error",
            "message": "Synology/NAS konnte nicht alle vorgesehenen Dateien bestätigen.",
            "details": details,
        }
    allowed = {"uploaded", "uploaded_conflict", "duplicate"}
    for entry in entries:
        if not isinstance(entry, Mapping) or entry.get("provider") != "synology_webdav":
            return {
                "state": "error",
                "message": "Synology/NAS lieferte keinen gültigen Uploadnachweis.",
                "details": details,
            }
        action = str(entry.get("action") or "").lower()
        if action not in allowed or not entry.get("content_sha256"):
            return {
                "state": "error",
                "message": "Synology/NAS konnte einen Upload nicht sicher bestätigen.",
                "details": details,
            }
        details["remote_files"].append(
            {
                "filename": str(entry.get("remote_filename") or entry.get("filename") or ""),
                "action": action,
                "remote_path": str(entry.get("remote_path") or ""),
            }
        )
    details["confirmed_count"] = expected_count
    return {
        "state": "success",
        "message": f"Synology/NAS bestätigt: {expected_count} Datei(en) synchronisiert oder identisch vorhanden.",
        "details": details,
    }
