import os
import json
import hashlib
import logging
import mimetypes
import uuid
from collections.abc import Iterable, Mapping
from pathlib import Path
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from core.runtime_paths import harden_private_file, normalize_token_path

logger = logging.getLogger("UnifiedOCR")

# Scopes required to manage files on Google Drive
SCOPES = ['https://www.googleapis.com/auth/drive']

class GoogleDriveClient:
    def __init__(self):
        pass

    def get_credentials(self, token_path: str) -> Credentials | None:
        """Loads credentials from token_path if they exist and are valid/refreshable."""
        t_path = Path(normalize_token_path(token_path))
        if not t_path.exists():
            return None
        try:
            creds = Credentials.from_authorized_user_file(str(t_path), SCOPES)
            if creds and creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                    with open(t_path, 'w', encoding='utf-8') as token_file:
                        token_file.write(creds.to_json())
                    harden_private_file(t_path)
                except Exception as refresh_err:
                    logger.warning(f"Fehler beim Aktualisieren des Google-Drive-Tokens. Token wird gelöscht: {refresh_err}")
                    try:
                        t_path.unlink()
                    except Exception as unlink_err:
                        logger.error(f"Konnte ungültige Token-Datei nicht löschen: {unlink_err}")
                    return None
            return creds
        except Exception as e:
            logger.error(f"Fehler beim Laden/Aktualisieren der Google-Drive-Anmeldedaten: {e}")
            return None

    def is_authenticated(self, token_path: str) -> bool:
        """Returns True if valid credentials exist or were successfully refreshed."""
        creds = self.get_credentials(token_path)
        return creds is not None and creds.valid

    def get_authenticated_user_email(self, token_path: str) -> str | None:
        """Retrieves the email address of the authenticated Google account."""
        try:
            creds = self.get_credentials(token_path)
            if not creds:
                return None
            service = build('drive', 'v3', credentials=creds)
            about = service.about().get(fields="user(emailAddress)").execute()
            user_info = about.get('user', {})
            return user_info.get('emailAddress')
        except Exception as e:
            logger.error(f"Fehler beim Abrufen der Google-Benutzer-E-Mail: {e}")
            return None

    def authenticate(self, credentials_path: str, token_path: str) -> str:
        """
        Runs the local OAuth flow using credentials_path.
        Saves the resulting token to token_path.
        Returns the user's email upon successful authentication.
        """
        creds_p = Path(credentials_path)
        if not creds_p.exists():
            raise FileNotFoundError(f"Google credentials.json nicht gefunden unter: {credentials_path}")

        flow = InstalledAppFlow.from_client_secrets_file(str(creds_p), SCOPES)
        # Runs a local webserver to handle the OAuth redirect
        creds = flow.run_local_server(port=0, authorization_prompt_message="Bitte authentifizieren Sie sich im geöffneten Browser.")

        # Save the credentials for the next run
        t_path = Path(normalize_token_path(token_path))
        t_path.parent.mkdir(parents=True, exist_ok=True)
        with open(str(t_path), 'w', encoding='utf-8') as token:
            token.write(creds.to_json())
        harden_private_file(t_path)

        # Retrieve email to confirm success
        email = self.get_authenticated_user_email(str(t_path))
        if not email:
            raise RuntimeError("Authentifizierung erfolgreich, aber E-Mail-Adresse konnte nicht ermittelt werden.")
        return email

    def logout(self, token_path: str):
        """Removes the saved token.json to log the user out."""
        t_path = Path(normalize_token_path(token_path))
        if t_path.exists():
            try:
                t_path.unlink()
                logger.info(f"Google Drive Token gelöscht: {token_path}")
            except Exception as e:
                logger.error(f"Fehler beim Löschen des Google Drive Tokens: {e}")
                raise

    def _get_service(self, token_path: str):
        creds = self.get_credentials(token_path)
        if not creds:
            return None
        return build('drive', 'v3', credentials=creds)

    def _escape_query_value(self, value: str) -> str:
        return value.replace("\\", "\\\\").replace("'", "\\'")

    @staticmethod
    def _md5_file(path: Path) -> str:
        digest = hashlib.md5()  # nosec B324 - Drive exposes MD5 as a content identifier.
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _conflict_filename(filename: str, index: int) -> str:
        path = Path(filename)
        return f"{path.stem}_conflict_{index:03d}{path.suffix}"

    @staticmethod
    def _matches_local_content(item: dict, *, md5_checksum: str, size: int) -> bool:
        if str(item.get("md5Checksum") or "").lower() != md5_checksum:
            return False
        remote_size = item.get("size")
        if remote_size in (None, ""):
            return True
        try:
            return int(remote_size) == size
        except (TypeError, ValueError):
            return False

    def _find_files(self, service, filename: str, parent_id: str) -> list[dict]:
        escaped_filename = self._escape_query_value(filename)
        query = (
            f"name = '{escaped_filename}' and '{parent_id}' in parents and "
            "mimeType != 'application/vnd.google-apps.folder' and trashed = false"
        )
        results = service.files().list(
            q=query,
            fields="files(id, name, parents, md5Checksum, size, mimeType, trashed, appProperties)",
        ).execute()
        return results.get("files", [])

    def _find_folders(self, service, folder_name: str, parent_id: str = None) -> list[dict]:
        escaped_name = self._escape_query_value(folder_name)
        parent_part = f"'{parent_id}' in parents" if parent_id else "'root' in parents"
        query = f"name = '{escaped_name}' and mimeType = 'application/vnd.google-apps.folder' and {parent_part} and trashed = false"
        results = service.files().list(q=query, fields="files(id, name, parents)").execute()
        return results.get('files', [])

    def _create_folder(self, service, folder_name: str, parent_id: str = None) -> str:
        folder_metadata = {
            'name': folder_name,
            'mimeType': 'application/vnd.google-apps.folder'
        }
        if parent_id:
            folder_metadata['parents'] = [parent_id]
        folder = service.files().create(body=folder_metadata, fields='id').execute()
        return folder.get('id')

    def get_folder_metadata(self, service, folder_id: str) -> dict | None:
        if not folder_id or folder_id == "root":
            return {"id": "root", "name": "root"}
        try:
            return service.files().get(fileId=folder_id, fields="id, name, parents, trashed, mimeType").execute()
        except Exception as e:
            logger.warning(f"Google-Drive-Ordner konnte nicht gelesen werden ({folder_id}): {e}")
            return None

    @staticmethod
    def _get_root_folder_id(service) -> str:
        try:
            metadata = service.files().get(fileId="root", fields="id").execute()
            root_id = str(metadata.get("id") or "").strip()
        except Exception as exc:
            raise RuntimeError(
                "Google-Drive-Wurzelordner konnte fuer die sichere Elternpruefung "
                f"nicht gelesen werden: {exc}"
            ) from exc
        if not root_id:
            raise RuntimeError(
                "Google Drive lieferte keine ID fuer den Wurzelordner; Upload wird blockiert."
            )
        return root_id

    def _get_or_create_folder(self, service, folder_name: str, parent_id: str = None) -> str:
        """Queries Google Drive for a folder with the given name, creating it if not found."""
        files = self._find_folders(service, folder_name, parent_id)
        if files:
            return files[0]['id']

        return self._create_folder(service, folder_name, parent_id)

    def ensure_folder_path(self, service, relative_path: str, known_ids: dict | None = None) -> dict:
        """
        Ensures a Drive folder hierarchy exists and returns IDs plus a small audit trail.
        Existing IDs are trusted only if Drive still returns metadata for them.
        """
        if not relative_path or relative_path in [".", "/"]:
            return {"folder_id": "root", "created": [], "found": [], "conflicts": [], "path_ids": {}}

        parts = [p.strip() for p in relative_path.replace("\\", "/").split("/") if p.strip()]
        parent_id = None
        path_ids = {}
        created = []
        found = []
        conflicts = []
        known_ids = known_ids or {}

        current_parts = []
        for part in parts:
            current_parts.append(part)
            current_path = "/".join(current_parts)

            known_id = known_ids.get(current_path)
            if known_id:
                meta = self.get_folder_metadata(service, known_id)
                expected_parent_id = parent_id
                if meta and expected_parent_id is None:
                    expected_parent_id = self._get_root_folder_id(service)
                expected_parent_matches = (
                    expected_parent_id in (meta.get("parents") or [])
                ) if meta else False
                if (
                    meta
                    and not meta.get("trashed")
                    and meta.get("mimeType") == "application/vnd.google-apps.folder"
                    and str(meta.get("name") or "").casefold() == part.casefold()
                    and expected_parent_matches
                ):
                    parent_id = known_id if known_id != "root" else None
                    path_ids[current_path] = known_id
                    found.append(current_path)
                    continue
                conflict = {
                    "path": current_path,
                    "message": "Gespeicherte Drive-Ordner-ID passt nicht mehr zu Name oder Elternordner.",
                    "folder_ids": [known_id],
                    "blocking": True,
                }
                conflicts.append(conflict)
                raise RuntimeError(
                    f"Gespeicherte Google-Drive-Ordner-ID fuer '{current_path}' ist "
                    "veraltet oder passt nicht zu Name/Elternordner; Upload wird "
                    f"geschlossen blockiert (ID: {known_id})."
                )

            matches = self._find_folders(service, part, parent_id)
            if len(matches) > 1:
                conflict = {
                    "path": current_path,
                    "message": f"Mehrere Drive-Ordner namens '{part}' unter demselben Elternordner gefunden.",
                    "folder_ids": [m.get("id") for m in matches],
                    "blocking": True,
                }
                conflicts.append(conflict)
                raise RuntimeError(
                    f"Mehrdeutige Google-Drive-Ordnerzuordnung für '{current_path}': "
                    + ", ".join(str(folder_id) for folder_id in conflict["folder_ids"])
                )

            if matches:
                folder_id = matches[0]["id"]
                found.append(current_path)
            else:
                folder_id = self._create_folder(service, part, parent_id)
                created.append(current_path)

            path_ids[current_path] = folder_id
            parent_id = folder_id

        return {
            "folder_id": parent_id or "root",
            "created": created,
            "found": found,
            "conflicts": conflicts,
            "path_ids": path_ids,
        }

    def rename_folder(self, service, folder_id: str, new_name: str) -> str:
        updated = service.files().update(fileId=folder_id, body={"name": new_name}, fields="id").execute()
        return updated.get("id")

    def move_folder(self, service, folder_id: str, new_parent_id: str, old_parent_ids: list[str] | None = None) -> str:
        remove_parents = ",".join(old_parent_ids or [])
        updated = service.files().update(
            fileId=folder_id,
            addParents=new_parent_id,
            removeParents=remove_parents,
            fields="id, parents",
        ).execute()
        return updated.get("id")

    def _resolve_path_to_folder_id(
        self,
        service,
        relative_path: str,
        known_ids: dict | None = None,
    ) -> str:
        """Resolve an upload folder without selecting an ambiguous name match."""
        resolution = self.ensure_folder_path(service, relative_path, known_ids=known_ids)
        return str(resolution.get("folder_id") or "root")

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
            raise ValueError("Das Google-Drive-Uploadpaket enthält keine Dateien.")
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
                "md5": GoogleDriveClient._md5_file(path),
                "size": path.stat().st_size,
            })
        return package_stem, members

    @staticmethod
    def _candidate_package_stem(package_stem: str, index: int) -> str:
        return package_stem if index == 0 else f"{package_stem}_conflict_{index:03d}"

    @staticmethod
    def _candidate_member_name(candidate_stem: str, member: dict) -> str:
        return f"{candidate_stem}{member['role_suffix']}{member['path'].suffix}"

    def _create_remote_file(
        self,
        service,
        local: Path,
        remote_filename: str,
        parent_id: str,
        *,
        app_properties: dict[str, str] | None = None,
    ) -> tuple[str | None, str]:
        mime_type, _ = mimetypes.guess_type(str(local))
        mime_type = mime_type or "application/octet-stream"
        media = MediaFileUpload(str(local), mimetype=mime_type, resumable=True)
        body = {"name": remote_filename, "parents": [parent_id]}
        if app_properties:
            body["appProperties"] = dict(app_properties)
        new_file = service.files().create(
            body=body,
            media_body=media,
            fields="id",
        ).execute()
        return new_file.get("id"), mime_type

    @staticmethod
    def _package_identity(parent_id: str, members: list[dict]) -> str:
        payload = {
            "parent_id": parent_id,
            "members": [
                {
                    "role": str(member["role"]),
                    "suffix": str(member["role_suffix"]),
                    "md5": str(member["md5"]),
                    "size": int(member["size"]),
                }
                for member in members
            ],
        }
        encoded = json.dumps(
            payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _upload_app_properties(
        *, package_id: str, role: str, content_md5: str, attempt_id: str
    ) -> dict[str, str]:
        return {
            "unifiedOcrPackage": package_id,
            "unifiedOcrRole": str(role)[:80],
            "unifiedOcrMd5": content_md5,
            "unifiedOcrAttempt": attempt_id,
        }

    @staticmethod
    def _created_metadata_matches(
        metadata: dict,
        *,
        file_id: str,
        remote_filename: str,
        parent_id: str,
        md5_checksum: str,
        size: int,
        app_properties: dict[str, str],
    ) -> bool:
        if not metadata or str(metadata.get("id") or "") != str(file_id):
            return False
        if metadata.get("trashed") or metadata.get("name") != remote_filename:
            return False
        if parent_id not in (metadata.get("parents") or []):
            return False
        if not GoogleDriveClient._matches_local_content(
            metadata, md5_checksum=md5_checksum, size=size
        ):
            return False
        remote_properties = metadata.get("appProperties") or {}
        return all(
            remote_properties.get(key) == value
            for key, value in app_properties.items()
        )

    def _get_file_metadata(self, service, file_id: str) -> dict:
        return service.files().get(
            fileId=file_id,
            fields="id, name, parents, md5Checksum, size, mimeType, trashed, appProperties",
        ).execute()

    def _rollback_created_files(self, service, created: list[dict]) -> list[dict]:
        """Delete only files provably created and tagged by this attempt."""
        audit: list[dict] = []
        errors: list[str] = []
        for record in reversed(created):
            file_id = str(record["file_id"])
            entry = {
                "drive_file_id": file_id,
                "remote_filename": record["remote_filename"],
                "action": "rollback_blocked",
            }
            try:
                metadata = self._get_file_metadata(service, file_id)
                if not self._created_metadata_matches(
                    metadata,
                    file_id=file_id,
                    remote_filename=record["remote_filename"],
                    parent_id=record["parent_id"],
                    md5_checksum=record["md5"],
                    size=record["size"],
                    app_properties=record["app_properties"],
                ):
                    raise RuntimeError("Datei-Eigenschaften stimmen nicht mehr ueberein")
                service.files().delete(fileId=file_id).execute()
                entry["action"] = "rolled_back"
            except Exception as exc:
                entry["error"] = str(exc)
                errors.append(f"{file_id}: {exc}")
            audit.append(entry)
        if errors:
            error = RuntimeError(
                "Google-Drive-Race erkannt, aber der sichere Rollback war unvollstaendig: "
                + "; ".join(errors)
            )
            error.rollback_audit = audit
            raise error
        return audit

    def _abort_created_attempt(
        self,
        service,
        created: list[dict],
        message: str,
        *,
        cause: Exception | None = None,
    ) -> None:
        audit = self._rollback_created_files(service, created)
        error = RuntimeError(message)
        error.rollback_audit = audit
        if cause is not None:
            raise error from cause
        raise error

    def upload_package_with_audit(
        self,
        token_path: str,
        local_paths: Mapping[str, str | Path] | Iterable[str | Path],
        relative_dest_path: str,
        *,
        known_ids: dict | None = None,
    ) -> list[dict]:
        """Safely create/reuse all members under one shared remote basename.

        Every candidate is planned before the first write. If one member has
        different content, all members advance to the same ``_conflict_NNN``
        stem. Identical members from a partial prior upload are reused and only
        missing members are created. This method never calls Drive ``update``.
        """
        items = self._normalise_package_paths(local_paths)
        package_stem, members = self._package_layout(items)
        service = self._get_service(token_path)
        if not service:
            raise ValueError("Keine gültige Verbindung zu Google Drive vorhanden (nicht authentifiziert).")
        if known_ids is None:
            parent_id = self._resolve_path_to_folder_id(service, relative_dest_path)
        else:
            parent_id = self._resolve_path_to_folder_id(
                service, relative_dest_path, known_ids=known_ids
            )

        package_id = self._package_identity(parent_id, members)
        attempt_id = uuid.uuid4().hex
        conflict_ids: list[str] = []
        rollback_audit: list[dict] = []
        for index in range(0, 10_000):
            candidate_stem = self._candidate_package_stem(package_stem, index)
            planned = []
            compatible = True
            for member in members:
                remote_filename = self._candidate_member_name(candidate_stem, member)
                existing = self._find_files(service, remote_filename, parent_id)
                if len(existing) > 1:
                    # Duplicate Drive names are intrinsically ambiguous. Never
                    # pick the first result, even if all checksums are equal.
                    compatible = False
                    conflict_ids.extend(
                        str(item.get("id")) for item in existing if item.get("id")
                    )
                    break
                identical = [
                    item for item in existing
                    if self._matches_local_content(
                        item, md5_checksum=member["md5"], size=member["size"]
                    )
                ]
                if existing and len(identical) != len(existing):
                    compatible = False
                    if index == 0:
                        conflict_ids.extend(
                            str(item.get("id")) for item in existing if item.get("id")
                        )
                    break
                planned.append({
                    **member,
                    "remote_filename": remote_filename,
                    "existing": identical,
                })
            if not compatible:
                continue

            audits = []
            created_in_attempt: list[dict] = []
            restart_with_conflict = False
            for member in planned:
                local = member["path"]
                remote_filename = member["remote_filename"]
                existing = member["existing"]
                post_create_verified = False
                if existing:
                    remote = existing[0]
                    file_id = remote.get("id")
                    mime_type = remote.get("mimeType") or None
                    action = "duplicate"
                else:
                    app_properties = self._upload_app_properties(
                        package_id=package_id,
                        role=member["role"],
                        content_md5=member["md5"],
                        attempt_id=attempt_id,
                    )
                    try:
                        file_id, mime_type = self._create_remote_file(
                            service,
                            local,
                            remote_filename,
                            parent_id,
                            app_properties=app_properties,
                        )
                    except Exception as create_exc:
                        self._abort_created_attempt(
                            service,
                            created_in_attempt,
                            f"Google-Drive-Create fuer '{remote_filename}' ist fehlgeschlagen; "
                            "bereits bestaetigte Paketmitglieder wurden zurueckgerollt.",
                            cause=create_exc,
                        )
                    if not file_id:
                        self._abort_created_attempt(
                            service,
                            created_in_attempt,
                            f"Google Drive bestaetigte fuer '{remote_filename}' keine Datei-ID; "
                            "der Upload wird geschlossen blockiert.",
                        )
                    created_record = {
                        "file_id": str(file_id),
                        "remote_filename": remote_filename,
                        "parent_id": parent_id,
                        "md5": member["md5"],
                        "size": member["size"],
                        "app_properties": app_properties,
                    }
                    created_in_attempt.append(created_record)
                    try:
                        metadata = self._get_file_metadata(service, str(file_id))
                        post_create_verified = self._created_metadata_matches(
                            metadata,
                            file_id=str(file_id),
                            remote_filename=remote_filename,
                            parent_id=parent_id,
                            md5_checksum=member["md5"],
                            size=member["size"],
                            app_properties=app_properties,
                        )
                        same_name = self._find_files(
                            service, remote_filename, parent_id
                        )
                    except Exception as verify_exc:
                        self._abort_created_attempt(
                            service,
                            created_in_attempt,
                            f"Google-Drive-Datei '{remote_filename}' konnte nach Create "
                            "nicht sicher verifiziert werden.",
                            cause=verify_exc,
                        )
                    unique_created_name = (
                        len(same_name) == 1
                        and str(same_name[0].get("id") or "") == str(file_id)
                    )
                    if not post_create_verified or not unique_created_name:
                        conflict_ids.extend(
                            str(item.get("id")) for item in same_name if item.get("id")
                        )
                        rollback_audit.extend(
                            self._rollback_created_files(service, created_in_attempt)
                        )
                        restart_with_conflict = True
                        break
                    action = "created_conflict" if index else "created"
                audits.append({
                    "provider": "google_drive",
                    "role": member["role"],
                    "local_path": str(local),
                    "filename": local.name,
                    "remote_filename": remote_filename,
                    "folder_path": relative_dest_path,
                    "drive_folder_id": parent_id,
                    "drive_file_id": file_id,
                    "action": action,
                    "mime_type": mime_type,
                    "content_md5": member["md5"],
                    "content_size": member["size"],
                    "package_id": package_id,
                    "post_create_verified": post_create_verified,
                    "package_stem": candidate_stem,
                    "conflict_with_ids": sorted(set(conflict_ids)),
                    "rollback_audit": list(rollback_audit),
                })
            if restart_with_conflict:
                continue
            return audits

        raise RuntimeError(
            f"Kein freier archivfester Paketname für '{package_stem}' in Google Drive gefunden."
        )

    def upload_file(self, token_path: str, local_path: str, relative_dest_path: str, *, known_ids: dict | None = None) -> str:
        """
        Uploads a file to a specific folder on Google Drive.
        Existing files are never overwritten. Byte-identical content is reused;
        a name conflict with different or unverifiable content receives a safe
        ``_conflict_NNN`` name.
        Returns the uploaded file ID.
        """
        return self.upload_file_with_audit(
            token_path, local_path, relative_dest_path, known_ids=known_ids
        )["drive_file_id"]

    def upload_file_with_audit(self, token_path: str, local_path: str, relative_dest_path: str, *, known_ids: dict | None = None) -> dict:
        """
        Uploads a file and returns manifest-ready audit metadata.

        ``action`` is ``created``, ``created_conflict`` or ``duplicate``. The
        method deliberately never calls Drive's content-update endpoint.
        """
        return self.upload_package_with_audit(
            token_path,
            [local_path],
            relative_dest_path,
            known_ids=known_ids,
        )[0]
