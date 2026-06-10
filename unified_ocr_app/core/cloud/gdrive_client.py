import os
import json
import logging
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
                if meta and not meta.get("trashed") and meta.get("mimeType") == "application/vnd.google-apps.folder":
                    parent_id = known_id if known_id != "root" else None
                    path_ids[current_path] = known_id
                    found.append(current_path)
                    continue

            matches = self._find_folders(service, part, parent_id)
            if len(matches) > 1:
                conflicts.append({
                    "path": current_path,
                    "message": f"Mehrere Drive-Ordner namens '{part}' unter demselben Elternordner gefunden. Erster Treffer wird verwendet.",
                    "folder_ids": [m.get("id") for m in matches],
                })

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

    def _resolve_path_to_folder_id(self, service, relative_path: str) -> str:
        """Resolves a relative path (e.g. 'Laura/Auto') to a Google Drive folder ID, creating folders as needed."""
        if not relative_path or relative_path in [".", "/"]:
            return 'root'

        # Support both forward and backward slashes
        parts = [p.strip() for p in relative_path.replace("\\", "/").split("/") if p.strip()]
        parent_id = None
        for part in parts:
            parent_id = self._get_or_create_folder(service, part, parent_id)
        return parent_id or 'root'

    def upload_file(self, token_path: str, local_path: str, relative_dest_path: str) -> str:
        """
        Uploads a file to a specific folder on Google Drive.
        If a file with the same name already exists in that folder, updates it instead of duplicating.
        Returns the uploaded file ID.
        """
        l_path = Path(local_path)
        if not l_path.exists():
            raise FileNotFoundError(f"Lokale Datei existiert nicht: {local_path}")

        service = self._get_service(token_path)
        if not service:
            raise ValueError("Keine gültige Verbindung zu Google Drive vorhanden (nicht authentifiziert).")

        # Resolve destination folder ID on Drive
        parent_id = self._resolve_path_to_folder_id(service, relative_dest_path)

        filename = l_path.name
        escaped_filename = filename.replace("'", "\\'")

        # Query for existing file in the folder to prevent duplicates
        query = f"name = '{escaped_filename}' and '{parent_id}' in parents and trashed = false"
        results = service.files().list(q=query, fields="files(id, name)").execute()
        existing_files = results.get('files', [])

        import mimetypes
        mime_type, _ = mimetypes.guess_type(str(l_path))
        if not mime_type:
            mime_type = 'application/octet-stream'

        media = MediaFileUpload(str(l_path), mimetype=mime_type, resumable=True)

        if existing_files:
            file_id = existing_files[0]['id']
            logger.info(f"Aktualisiere existierende Datei '{filename}' (ID: {file_id}) in Google Drive Ordner ID {parent_id}")
            updated_file = service.files().update(
                fileId=file_id,
                media_body=media
            ).execute()
            return updated_file.get('id')
        else:
            logger.info(f"Erstelle neue Datei '{filename}' in Google Drive Ordner ID {parent_id}")
            file_metadata = {
                'name': filename,
                'parents': [parent_id]
            }
            new_file = service.files().create(
                body=file_metadata,
                media_body=media,
                fields='id'
            ).execute()
            return new_file.get('id')
