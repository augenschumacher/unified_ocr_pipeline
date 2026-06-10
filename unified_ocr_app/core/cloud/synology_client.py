from __future__ import annotations

import ipaddress
import mimetypes
from pathlib import Path
from urllib.parse import quote, urlsplit

import requests


def is_private_webdav_url(url: str) -> bool:
    """Return True for localhost, private IPs, simple LAN names, and .local hosts."""
    try:
        host = urlsplit(url).hostname or ""
    except Exception:
        return False
    if not host:
        return False
    lowered = host.lower()
    if lowered in {"localhost"} or lowered.endswith(".local"):
        return True
    if "." not in lowered:
        return True
    try:
        ip = ipaddress.ip_address(lowered)
        return ip.is_private or ip.is_loopback or ip.is_link_local
    except ValueError:
        return False


class SynologyWebDAVClient:
    """
    Minimal WebDAV client for Synology WebDAV Server.

    Synology typically exposes WebDAV on ports 5005 (HTTP) or 5006 (HTTPS).
    The client intentionally avoids storing or logging credentials; callers
    provide them at runtime from settings or environment variables.
    """

    def __init__(
        self,
        *,
        base_url: str,
        username: str = "",
        password: str = "",
        root_path: str = "",
        timeout: int = 30,
        verify_tls: bool = True,
        session=None,
    ):
        self.base_url = (base_url or "").strip().rstrip("/")
        self.username = (username or "").strip()
        self.password = password or ""
        self.root_path = self._clean_relative_path(root_path)
        self.timeout = timeout
        self.verify_tls = verify_tls
        self.session = session or requests.Session()

    @property
    def is_configured(self) -> bool:
        return bool(self.base_url and self.username and self.password)

    @staticmethod
    def _clean_relative_path(path: str) -> str:
        parts = []
        for part in str(path or "").replace("\\", "/").split("/"):
            cleaned = part.strip()
            if cleaned and cleaned not in {".", ".."}:
                parts.append(cleaned)
        return "/".join(parts)

    @staticmethod
    def _quote_path(path: str) -> str:
        return "/".join(quote(part, safe="") for part in path.split("/") if part)

    def _url_for(self, relative_path: str = "") -> str:
        cleaned = self._clean_relative_path(relative_path)
        combined = "/".join(part for part in (self.root_path, cleaned) if part)
        if combined:
            return f"{self.base_url}/{self._quote_path(combined)}"
        return self.base_url

    def _request(self, method: str, url: str, **kwargs):
        auth = (self.username, self.password) if self.username or self.password else None
        return self.session.request(
            method,
            url,
            auth=auth,
            timeout=self.timeout,
            verify=self.verify_tls,
            **kwargs,
        )

    def test_connection(self) -> bool:
        if not self.is_configured:
            return False
        response = self._request("PROPFIND", self._url_for(), headers={"Depth": "0"})
        return response.status_code in {200, 207}

    def ensure_folder(self, relative_path: str) -> list[str]:
        """
        Ensure a remote folder path exists.

        Returns the folders that were created. Existing folders are accepted.
        """
        created = []
        cleaned = self._clean_relative_path(relative_path)
        if not cleaned:
            return created

        current_parts = []
        for part in cleaned.split("/"):
            current_parts.append(part)
            current_path = "/".join(current_parts)
            url = self._url_for(current_path)
            probe = self._request("PROPFIND", url, headers={"Depth": "0"})
            if probe.status_code in {200, 207}:
                continue
            response = self._request("MKCOL", url)
            if response.status_code in {200, 201, 405}:
                if response.status_code != 405:
                    created.append(current_path)
                continue
            raise RuntimeError(
                f"Synology-Ordner konnte nicht erstellt werden ({current_path}): "
                f"HTTP {response.status_code} {response.text[:200]}"
            )
        return created

    def upload_file(self, local_path: str | Path, relative_dest_path: str) -> dict:
        local = Path(local_path)
        if not local.exists():
            raise FileNotFoundError(f"Lokale Datei existiert nicht: {local}")
        if not self.is_configured:
            raise ValueError("Synology WebDAV ist nicht vollständig konfiguriert.")

        dest_path = self._clean_relative_path(relative_dest_path)
        created_folders = self.ensure_folder(dest_path)
        remote_relative = "/".join(part for part in (dest_path, local.name) if part)
        url = self._url_for(remote_relative)
        mime_type, _ = mimetypes.guess_type(str(local))
        headers = {"Content-Type": mime_type or "application/octet-stream"}
        with local.open("rb") as fh:
            response = self._request("PUT", url, data=fh, headers=headers)
        if response.status_code not in {200, 201, 204}:
            raise RuntimeError(
                f"Synology-Upload fehlgeschlagen ({local.name}): "
                f"HTTP {response.status_code} {response.text[:200]}"
            )
        return {
            "provider": "synology_webdav",
            "local_path": str(local),
            "filename": local.name,
            "folder_path": dest_path,
            "remote_path": remote_relative,
            "created_folders": created_folders,
            "action": "uploaded" if response.status_code == 201 else "updated",
        }
