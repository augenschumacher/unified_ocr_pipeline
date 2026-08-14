from __future__ import annotations

import base64
import hashlib
import ipaddress
import mimetypes
import re
from collections.abc import Iterable, Mapping
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

    @staticmethod
    def _file_digests(path: Path) -> dict[str, str]:
        md5_digest = hashlib.md5()  # nosec B324 - interoperability content identifier.
        sha256_digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                md5_digest.update(chunk)
                sha256_digest.update(chunk)
        return {
            "md5": md5_digest.hexdigest(),
            "sha-256": sha256_digest.hexdigest(),
        }

    @staticmethod
    def _conflict_filename(filename: str, index: int) -> str:
        path = Path(filename)
        return f"{path.stem}_conflict_{index:03d}{path.suffix}"

    @staticmethod
    def _response_headers(response) -> dict[str, str]:
        return {
            str(key).lower(): str(value).strip()
            for key, value in (getattr(response, "headers", None) or {}).items()
        }

    @classmethod
    def _remote_digest(cls, response) -> tuple[str, str] | None:
        """Return a trustworthy server-provided digest when WebDAV exposes one."""
        headers = cls._response_headers(response)

        content_md5 = headers.get("content-md5")
        if content_md5:
            try:
                return "md5", base64.b64decode(content_md5, validate=True).hex()
            except (ValueError, TypeError):
                pass

        for header in ("x-checksum-sha256", "x-checksum-md5"):
            value = headers.get(header, "").strip().lower()
            algorithm = "sha-256" if header.endswith("sha256") else "md5"
            expected_length = 64 if algorithm == "sha-256" else 32
            if len(value) == expected_length and re.fullmatch(r"[0-9a-f]+", value):
                return algorithm, value

        # RFC Digest values are normally base64 encoded. Colons are accepted
        # for the structured-field representation used by newer servers.
        for item in headers.get("digest", "").split(","):
            if "=" not in item:
                continue
            raw_algorithm, raw_value = item.split("=", 1)
            algorithm = raw_algorithm.strip().lower()
            if algorithm not in {"sha-256", "md5"}:
                continue
            encoded = raw_value.strip().strip('"').strip(":")
            try:
                return algorithm, base64.b64decode(encoded, validate=True).hex()
            except (ValueError, TypeError):
                continue
        return None

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

    @staticmethod
    def _normalise_package_paths(
        local_paths: Mapping[str, str | Path] | Iterable[str | Path],
    ) -> list[tuple[str, Path]]:
        if isinstance(local_paths, Mapping):
            raw = [(str(role), Path(path)) for role, path in local_paths.items() if path]
        else:
            raw = [("", Path(path)) for path in local_paths if path]
        items: list[tuple[str, Path]] = []
        seen: set[Path] = set()
        for role, path in raw:
            resolved = path.resolve(strict=False)
            if resolved in seen:
                continue
            if not path.is_file():
                raise FileNotFoundError(f"Lokale Datei existiert nicht: {path}")
            seen.add(resolved)
            items.append((role or path.suffix.lstrip(".") or "artifact", path))
        if not items:
            raise ValueError("Das Synology-Uploadpaket enthält keine Dateien.")
        return items

    @staticmethod
    def _package_layout(items: list[tuple[str, Path]]) -> tuple[str, list[dict]]:
        from core.cloud.organizer import DocumentOrganizer

        layouts = DocumentOrganizer._package_layouts(items)
        package_stems = {stem for stem, _suffix in layouts}
        if len(package_stems) != 1:
            raise ValueError(
                "Die Dateien lassen sich keinem eindeutigen Dokumentpaket zuordnen: "
                + ", ".join(path.name for _role, path in items)
            )
        package_stem = next(iter(package_stems))
        members = []
        for (role, path), (_stem, role_suffix) in zip(items, layouts):
            members.append({
                "role": role,
                "path": path,
                "role_suffix": role_suffix,
                "digests": SynologyWebDAVClient._file_digests(path),
            })
        return package_stem, members

    @staticmethod
    def _candidate_package_stem(package_stem: str, index: int) -> str:
        return package_stem if index == 0 else f"{package_stem}_conflict_{index:03d}"

    @staticmethod
    def _candidate_member_name(candidate_stem: str, member: dict) -> str:
        return f"{candidate_stem}{member['role_suffix']}{member['path'].suffix}"

    @classmethod
    def _response_matches_member(cls, response, member: dict) -> bool:
        remote_digest = cls._remote_digest(response)
        return bool(
            remote_digest
            and member["digests"].get(remote_digest[0]) == remote_digest[1]
        )

    def _rollback_created_members(self, created: list[dict]) -> tuple[list[dict], bool]:
        """Conditionally delete members provably unchanged since this attempt.

        A digest match proves the bytes still belong to this upload and an
        ETag-backed ``If-Match`` prevents deleting a concurrent replacement.
        Without both pieces of evidence the client deliberately leaves the
        object in place and reports a blocked rollback.
        """
        audit: list[dict] = []
        complete = True
        for record in reversed(created):
            entry = {
                "remote_path": record["remote_relative"],
                "action": "rollback_blocked",
            }
            try:
                probe = self._request(
                    "PROPFIND", record["url"], headers={"Depth": "0"}
                )
                if probe.status_code == 404:
                    entry["action"] = "already_absent"
                    audit.append(entry)
                    continue
                if probe.status_code not in {200, 207}:
                    raise RuntimeError(f"PROPFIND HTTP {probe.status_code}")
                if not self._response_matches_member(probe, record):
                    raise RuntimeError("Remote-Inhalt stimmt nicht mehr mit dem Upload ueberein")
                headers = self._response_headers(probe)
                etag = headers.get("etag") or record.get("etag")
                if not etag:
                    raise RuntimeError("Kein ETag fuer einen bedingten DELETE verfuegbar")
                response = self._request(
                    "DELETE", record["url"], headers={"If-Match": etag}
                )
                if response.status_code not in {200, 204, 404}:
                    raise RuntimeError(f"DELETE HTTP {response.status_code}")
                entry["action"] = (
                    "already_absent" if response.status_code == 404 else "rolled_back"
                )
                entry["if_match"] = etag
            except Exception as exc:
                complete = False
                entry["error"] = str(exc)
            audit.append(entry)
        return audit, complete

    @staticmethod
    def _upload_error(message: str, rollback_audit: list[dict]) -> RuntimeError:
        error = RuntimeError(message)
        error.rollback_audit = list(rollback_audit)
        return error

    def upload_package_with_audit(
        self,
        local_paths: Mapping[str, str | Path] | Iterable[str | Path],
        relative_dest_path: str,
    ) -> list[dict]:
        """Conditionally create one coherent WebDAV document package.

        A candidate basename is selected for the whole package before upload.
        It is accepted only when every member is missing or digest-identical.
        This makes partial retries idempotent whenever the server exposes a
        trustworthy checksum, and prevents a conflict in one sidecar from
        splitting the package across unrelated names.
        """
        if not self.is_configured:
            raise ValueError("Synology WebDAV ist nicht vollständig konfiguriert.")
        items = self._normalise_package_paths(local_paths)
        package_stem, members = self._package_layout(items)
        dest_path = self._clean_relative_path(relative_dest_path)
        created_folders = self.ensure_folder(dest_path)
        conflict_with: list[str] = []
        rollback_audit: list[dict] = []

        for index in range(0, 10_000):
            candidate_stem = self._candidate_package_stem(package_stem, index)
            planned = []
            compatible = True
            for member in members:
                remote_filename = self._candidate_member_name(candidate_stem, member)
                remote_relative = "/".join(
                    part for part in (dest_path, remote_filename) if part
                )
                url = self._url_for(remote_relative)
                probe = self._request("PROPFIND", url, headers={"Depth": "0"})
                if probe.status_code == 404:
                    state = "missing"
                elif probe.status_code in {200, 207}:
                    if self._response_matches_member(probe, member):
                        state = "identical"
                    else:
                        compatible = False
                        conflict_with.append(remote_relative)
                        break
                else:
                    raise RuntimeError(
                        f"Synology-Zieldatei konnte nicht sicher geprueft werden ({remote_relative}): "
                        f"HTTP {probe.status_code} {probe.text[:200]}"
                    )
                planned.append({
                    **member,
                    "remote_filename": remote_filename,
                    "remote_relative": remote_relative,
                    "url": url,
                    "state": state,
                })
            if not compatible:
                continue

            audits = []
            created_in_attempt: list[dict] = []
            restart_with_conflict = False
            for member in planned:
                local = member["path"]
                action = "duplicate"
                if member["state"] == "missing":
                    mime_type, _ = mimetypes.guess_type(str(local))
                    try:
                        with local.open("rb") as fh:
                            response = self._request(
                                "PUT",
                                member["url"],
                                data=fh,
                                headers={
                                    "Content-Type": mime_type or "application/octet-stream",
                                    "If-None-Match": "*",
                                },
                            )
                    except Exception as upload_exc:
                        attempt_audit, rollback_complete = self._rollback_created_members(
                            created_in_attempt
                        )
                        rollback_audit.extend(attempt_audit)
                        detail = (
                            "Synology-Uploadfehler; innerhalb dieses Versuchs erstellte "
                            "Paketmitglieder wurden sicher zurueckgerollt."
                            if rollback_complete
                            else "Synology-Uploadfehler; der sichere Rollback war unvollstaendig."
                        )
                        raise self._upload_error(
                            f"{detail} Ursache: {upload_exc}", rollback_audit
                        ) from upload_exc
                    if response.status_code == 412:
                        attempt_audit, rollback_complete = self._rollback_created_members(
                            created_in_attempt
                        )
                        rollback_audit.extend(attempt_audit)
                        if not rollback_complete:
                            raise self._upload_error(
                                "Synology-Namenskonflikt waehrend eines Teiluploads; "
                                "der bedingte Rollback war unvollstaendig und der Upload "
                                "wird geschlossen blockiert.",
                                rollback_audit,
                            )
                        conflict_with.append(member["remote_relative"])
                        restart_with_conflict = True
                        break
                    if response.status_code != 201:
                        attempt_audit, rollback_complete = self._rollback_created_members(
                            created_in_attempt
                        )
                        rollback_audit.extend(attempt_audit)
                        rollback_detail = (
                            "Rollback vollstaendig."
                            if rollback_complete
                            else "Sicherer Rollback unvollstaendig."
                        )
                        raise self._upload_error(
                            f"Synology-Upload wurde nicht als neue Datei bestaetigt "
                            f"({member['remote_filename']}): HTTP {response.status_code} "
                            f"{response.text[:200]}. {rollback_detail} Zur Vermeidung eines "
                            "stillen Ueberschreibens wird der Vorgang blockiert.",
                            rollback_audit,
                        )
                    created_in_attempt.append({
                        **member,
                        "etag": self._response_headers(response).get("etag"),
                    })
                    action = "uploaded_conflict" if index else "uploaded"
                audits.append({
                    "provider": "synology_webdav",
                    "role": member["role"],
                    "local_path": str(local),
                    "filename": local.name,
                    "remote_filename": member["remote_filename"],
                    "folder_path": dest_path,
                    "remote_path": member["remote_relative"],
                    "created_folders": created_folders,
                    "action": action,
                    "content_sha256": member["digests"]["sha-256"],
                    "package_stem": candidate_stem,
                    "conflict_with": list(dict.fromkeys(conflict_with)),
                    "rollback_audit": list(rollback_audit),
                })
            if restart_with_conflict:
                continue
            return audits

        raise RuntimeError(
            f"Kein freier archivfester Paketname für '{package_stem}' auf Synology gefunden."
        )

    def upload_file(self, local_path: str | Path, relative_dest_path: str) -> dict:
        return self.upload_package_with_audit([local_path], relative_dest_path)[0]
