from __future__ import annotations

import ctypes
import os
from ctypes import wintypes


APP_PREFIX = "UnifiedOCR"
REF_PREFIX = "credential://"
SYNOLOGY_PASSWORD_NAME = "synology_webdav_password"


def make_secret_ref(name: str) -> str:
    return f"{REF_PREFIX}{APP_PREFIX}/{name}"


def is_secret_ref(value: str | None) -> bool:
    return isinstance(value, str) and value.startswith(f"{REF_PREFIX}{APP_PREFIX}/")


def name_from_ref(value: str) -> str:
    if not is_secret_ref(value):
        return value
    return value.rsplit("/", 1)[-1]


def credential_store_available() -> bool:
    return os.name == "nt" and hasattr(ctypes, "windll")


def store_secret(name: str, secret: str) -> str | None:
    if not secret:
        return ""
    if not credential_store_available():
        return None
    if _windows_cred_write(_target_name(name), secret):
        return make_secret_ref(name)
    return None


def load_secret(value_or_ref: str | None) -> str:
    if not value_or_ref:
        return ""
    if not is_secret_ref(value_or_ref):
        return value_or_ref
    if not credential_store_available():
        return ""
    return _windows_cred_read(_target_name(name_from_ref(value_or_ref))) or ""


def delete_secret(name_or_ref: str | None) -> bool:
    if not name_or_ref or not credential_store_available():
        return False
    name = name_from_ref(name_or_ref)
    return _windows_cred_delete(_target_name(name))


def _target_name(name: str) -> str:
    safe_name = str(name or "").strip().replace("\\", "/").strip("/")
    return f"{APP_PREFIX}/{safe_name}"


class _CREDENTIALW(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(wintypes.BYTE)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


PCREDENTIALW = ctypes.POINTER(_CREDENTIALW)

CRED_TYPE_GENERIC = 1
CRED_PERSIST_LOCAL_MACHINE = 2


def _windows_cred_write(target_name: str, secret: str) -> bool:
    blob = secret.encode("utf-16-le")
    if len(blob) > 5120:
        raise ValueError("Secret ist zu gross fuer den Windows Credential Manager.")

    blob_buffer = ctypes.create_string_buffer(blob)
    credential = _CREDENTIALW()
    credential.Type = CRED_TYPE_GENERIC
    credential.TargetName = target_name
    credential.CredentialBlobSize = len(blob)
    credential.CredentialBlob = ctypes.cast(blob_buffer, ctypes.POINTER(wintypes.BYTE))
    credential.Persist = CRED_PERSIST_LOCAL_MACHINE
    credential.UserName = APP_PREFIX

    return bool(ctypes.windll.advapi32.CredWriteW(ctypes.byref(credential), 0))


def _windows_cred_read(target_name: str) -> str | None:
    credential_ptr = PCREDENTIALW()
    ok = ctypes.windll.advapi32.CredReadW(
        target_name,
        CRED_TYPE_GENERIC,
        0,
        ctypes.byref(credential_ptr),
    )
    if not ok:
        return None
    try:
        credential = credential_ptr.contents
        size = int(credential.CredentialBlobSize or 0)
        if size <= 0:
            return ""
        raw = ctypes.string_at(credential.CredentialBlob, size)
        return raw.decode("utf-16-le")
    finally:
        ctypes.windll.advapi32.CredFree(credential_ptr)


def _windows_cred_delete(target_name: str) -> bool:
    return bool(ctypes.windll.advapi32.CredDeleteW(target_name, CRED_TYPE_GENERIC, 0))
