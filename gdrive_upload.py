"""
Google Drive uploader for Pi TX.

Auth: Google Device Authorization Grant (RFC 8628) — no browser on the Pi needed.
The app sends GDRIVE_AUTH_URL → Pi generates a URL + user_code → user visits URL
on any browser and approves → Pi polls for token completion in the background.

Credentials: stored at ~/.pi-tx/gdrive_client_secrets.json (standard Google
"installed app" JSON), or via GDRIVE_CLIENT_ID / GDRIVE_CLIENT_SECRET env vars.

Token: persisted at ~/.pi-tx/gdrive_token.json and auto-refreshed when expired.

Upload structure:
  <configured folder>/
    2024-11-15/
      IMG_001.CR3
      IMG_001.JPG
    2024-11-16/
      ...
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

# ── Optional imports ──────────────────────────────────────────────────────────
DRIVE_AVAILABLE = False
# Sentinel values so tests can always patch `gdrive_upload.build` /
# `gdrive_upload.MediaFileUpload` regardless of whether google-api-python-client
# is installed.
build          = None  # type: ignore[assignment]
MediaFileUpload = None  # type: ignore[assignment]
try:
    from googleapiclient.discovery import build          # noqa: F811
    from googleapiclient.http import MediaFileUpload     # noqa: F811
    DRIVE_AVAILABLE = True
except ImportError:
    pass

GOOGLE_AUTH_AVAILABLE = False
# Sentinel values so tests can always patch `gdrive_upload.Credentials` /
# `gdrive_upload.GoogleRequest` regardless of whether google-auth is installed.
Credentials   = None  # type: ignore[assignment]
GoogleRequest = None  # type: ignore[assignment]
try:
    from google.oauth2.credentials import Credentials      # noqa: F811
    from google.auth.transport.requests import Request as GoogleRequest  # noqa: F811
    GOOGLE_AUTH_AVAILABLE = True
except ImportError:
    pass

REQUESTS_AVAILABLE = False
try:
    import requests as _requests
    REQUESTS_AVAILABLE = True
except ImportError:
    pass

# ── Constants ─────────────────────────────────────────────────────────────────
_CONFIG_DIR  = Path.home() / ".pi-tx"
_CREDS_FILE  = _CONFIG_DIR / "gdrive_client_secrets.json"
_TOKEN_FILE  = _CONFIG_DIR / "gdrive_token.json"
_SETTINGS_FILE = _CONFIG_DIR / "gdrive_settings.json"

_DRIVE_SCOPE     = "https://www.googleapis.com/auth/drive.file"
_DEVICE_CODE_URL = "https://oauth2.googleapis.com/device/code"
_TOKEN_URL       = "https://oauth2.googleapis.com/token"
_USERINFO_URL    = "https://www.googleapis.com/oauth2/v1/userinfo"

_IMAGE_MIMETYPES: dict[str, str] = {
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
    ".cr2":  "image/x-canon-cr2",
    ".cr3":  "image/x-canon-cr3",
    ".nef":  "image/x-nikon-nef",
    ".nrw":  "image/x-nikon-nrw",
    ".arw":  "image/x-sony-arw",
    ".srf":  "image/x-sony-arw",
    ".sr2":  "image/x-sony-arw",
    ".orf":  "image/x-olympus-orf",
    ".raf":  "image/x-fuji-raf",
    ".dng":  "image/x-adobe-dng",
    ".rw2":  "image/x-panasonic-rw2",
    ".pef":  "image/x-pentax-pef",
    ".x3f":  "image/x-sigma-x3f",
}


class GDriveUploader:
    """
    Manages Google Drive auth and uploads for Pi TX.

    Lifecycle:
      get_auth_url()       — start device flow, emit GDRIVE_AUTH_URL,<url>,<code>
      check_auth()         — emit current auth status
      set_folder(name)     — configure destination folder
      get_status()         — emit GDRIVE_STATUS,...
      upload_files(paths, date_str, progress_cb)  — synchronous upload (call from bg thread)
    """

    def __init__(self, send_response: Callable[[str], None]) -> None:
        self._send           = send_response
        self._lock           = threading.Lock()

        # OAuth2 credentials
        self._client_id      = ""
        self._client_secret  = ""
        self._token: dict | None = None

        # Upload state
        self._folder_name    = "Pi TX Photos"
        self._folder_id: str | None = None
        self._total_uploaded = 0
        self._busy           = False
        self._last_error     = ""
        self._email          = ""
        # Mutex so only one upload worker can run at a time.
        # acquire(blocking=False) used in upload_files — caller gets None if busy.
        self._upload_lock    = threading.Lock()

        # Device flow polling state
        self._device_code    = ""
        self._poll_interval  = 5

        _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        self._load_credentials()
        self._load_token()
        self._load_settings()

    # ── Properties ─────────────────────────────────────────────────────────────

    @property
    def has_credentials(self) -> bool:
        return bool(self._client_id and self._client_secret)

    @property
    def is_authed(self) -> bool:
        return self._token is not None and bool(self._token.get("access_token"))

    @property
    def busy(self) -> bool:
        return self._busy

    # ── Credential / token persistence ────────────────────────────────────────

    def _load_credentials(self) -> None:
        """Load client_id/secret from env vars or credential file."""
        cid  = os.environ.get("GDRIVE_CLIENT_ID", "")
        csec = os.environ.get("GDRIVE_CLIENT_SECRET", "")
        if cid and csec:
            self._client_id     = cid
            self._client_secret = csec
            return
        try:
            data = json.loads(_CREDS_FILE.read_text())
            # Support both "installed" key and flat format
            blob = data.get("installed") or data.get("web") or data
            self._client_id     = blob.get("client_id", "")
            self._client_secret = blob.get("client_secret", "")
        except Exception:
            pass

    def _load_token(self) -> None:
        try:
            self._token = json.loads(_TOKEN_FILE.read_text())
            self._email = self._token.get("email", "")
            # Tighten permissions on any existing token that may have been created
            # with a permissive umask (Pi default is typically 022 → 644).
            try:
                _TOKEN_FILE.chmod(0o600)
            except Exception:
                pass
        except Exception:
            self._token = None

    def _save_token(self, token: dict) -> None:
        """
        Write token atomically with mode 0600 so the refresh token is not world-readable.

        Converts the relative `expires_in` field (seconds from now) to an absolute
        `expiry_at` (epoch seconds) so token expiry can be detected reliably after a
        Pi reboot without knowing when the original grant was made.
        """
        # Make a shallow copy so we don't mutate the caller's dict
        token = dict(token)
        if "expires_in" in token and "expiry_at" not in token:
            token["expiry_at"] = time.time() + int(token.get("expires_in", 3600))
        self._token = token
        self._email = token.get("email", "")
        try:
            _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            tmp = _TOKEN_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(token, indent=2))
            tmp.chmod(0o600)     # restrict BEFORE rename so it's never briefly 644
            tmp.rename(_TOKEN_FILE)
        except Exception as exc:
            print(f"[GDrive] Token save failed: {exc}")

    def _load_settings(self) -> None:
        try:
            data = json.loads(_SETTINGS_FILE.read_text())
            self._folder_name    = data.get("folder_name", self._folder_name)
            self._total_uploaded = int(data.get("total_uploaded", 0))
        except Exception:
            pass

    def _save_settings(self) -> None:
        """Write settings atomically with mode 0600."""
        try:
            _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            tmp = _SETTINGS_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps({
                "folder_name":    self._folder_name,
                "total_uploaded": self._total_uploaded,
            }, indent=2))
            tmp.chmod(0o600)
            tmp.rename(_SETTINGS_FILE)
        except Exception as exc:
            print(f"[GDrive] Settings save failed: {exc}")

    # ── Auth ───────────────────────────────────────────────────────────────────

    def get_auth_url(self) -> None:
        """
        Start device authorization flow.
        Emits GDRIVE_AUTH_URL,<verify_url>,<user_code> then starts background polling.
        """
        if not REQUESTS_AVAILABLE:
            self._send("GDRIVE_ERR,REQUESTS_LIB_NOT_INSTALLED\n")
            return
        if not self.has_credentials:
            self._send("GDRIVE_ERR,NO_CREDENTIALS\n")
            return

        try:
            resp = _requests.post(_DEVICE_CODE_URL, data={
                "client_id": self._client_id,
                "scope":     _DRIVE_SCOPE,
            }, timeout=10)
            data = resp.json()
        except Exception as exc:
            print(f"[GDrive] Device code request failed: {exc}")
            self._send("GDRIVE_ERR,NETWORK_ERROR\n")
            return

        if "error" in data:
            err = str(data["error"]).replace(",", ";")
            self._send(f"GDRIVE_ERR,{err}\n")
            return

        device_code       = data.get("device_code", "")
        user_code         = data.get("user_code", "").strip().replace(" ", "-")
        verify_url        = data.get("verification_url", "https://google.com/device")
        expires_in        = int(data.get("expires_in", 1800))
        self._poll_interval = max(5, int(data.get("interval", 5)))
        self._device_code = device_code

        self._send(f"GDRIVE_AUTH_URL,{verify_url},{user_code}\n")
        print(f"[GDrive] Device auth started — user visits {verify_url} and enters {user_code}")

        # Background polling thread
        threading.Thread(
            target=self._poll_for_token,
            args=(device_code, expires_in),
            daemon=True,
            name="gdrive-poll",
        ).start()

    def check_auth(self) -> None:
        """Emit current auth status (used by GDRIVE_AUTH_CODE command)."""
        if self.is_authed:
            email = self._email or "unknown"
            self._send(f"GDRIVE_AUTH_OK,{email}\n")
        elif self._device_code:
            self._send("GDRIVE_AUTH_PENDING\n")
        else:
            self._send("GDRIVE_AUTH_REQUIRED\n")

    def cancel_auth(self) -> None:
        """Cancel any ongoing device flow polling."""
        self._device_code = ""
        self._send("GDRIVE_AUTH_CANCELLED\n")

    def _poll_for_token(self, device_code: str, expires_in: int) -> None:
        """Background: poll token endpoint until the user approves or it expires."""
        deadline = time.monotonic() + expires_in
        while time.monotonic() < deadline:
            if device_code != self._device_code:
                return   # cancelled or superseded by new auth request
            time.sleep(self._poll_interval)
            if device_code != self._device_code:
                return

            try:
                resp = _requests.post(_TOKEN_URL, data={
                    "client_id":     self._client_id,
                    "client_secret": self._client_secret,
                    "device_code":   device_code,
                    "grant_type":    "urn:ietf:params:oauth:grant-type:device_code",
                }, timeout=10)
                data = resp.json()
            except Exception:
                continue

            if "access_token" in data:
                # Fetch user email for display
                try:
                    info = _requests.get(
                        _USERINFO_URL,
                        headers={"Authorization": f"Bearer {data['access_token']}"},
                        timeout=5,
                    ).json()
                    data["email"] = info.get("email", "unknown")
                except Exception:
                    data["email"] = "unknown"
                self._save_token(data)
                self._device_code = ""
                email = data["email"]
                self._send(f"GDRIVE_AUTH_OK,{email}\n")
                print(f"[GDrive] Authorized as {email}")
                return

            err = data.get("error", "")
            if err == "authorization_pending":
                continue
            elif err == "slow_down":
                self._poll_interval = min(30, self._poll_interval + 5)
            elif err == "expired_token":
                self._device_code = ""
                self._send("GDRIVE_AUTH_EXPIRED\n")
                return
            elif err:
                self._device_code = ""
                self._send(f"GDRIVE_ERR,{err.replace(',', ';')}\n")
                return

        # Timed out
        if device_code == self._device_code:
            self._device_code = ""
            self._send("GDRIVE_AUTH_EXPIRED\n")

    def _maybe_refresh_token(self) -> bool:
        """
        Refresh the access token if it has expired.
        Returns True if the token is (or became) valid, False if reauth is required.

        Uses the `expiry_at` absolute epoch timestamp saved by `_save_token` to
        decide whether the token is expired — this is reliable across Pi reboots
        because it does not depend on knowing the original grant time.
        """
        if not self._token:
            return False
        if not GOOGLE_AUTH_AVAILABLE or not REQUESTS_AVAILABLE:
            # Can't refresh without the google-auth library; treat token as valid
            # if we have an access_token (best-effort).
            return bool(self._token.get("access_token"))
        try:
            # Reconstruct a proper absolute expiry for Credentials so creds.expired
            # reflects reality rather than defaulting to False (which would skip refresh).
            expiry_at = self._token.get("expiry_at")
            expiry = (
                datetime.fromtimestamp(expiry_at, tz=timezone.utc)
                if expiry_at
                else None
            )
            creds = Credentials(
                token=self._token.get("access_token"),
                refresh_token=self._token.get("refresh_token"),
                token_uri=_TOKEN_URL,
                client_id=self._client_id,
                client_secret=self._client_secret,
                scopes=[_DRIVE_SCOPE],
                expiry=expiry,
            )
            if creds.expired and creds.refresh_token:
                print("[GDrive] Access token expired — refreshing via refresh_token")
                creds.refresh(GoogleRequest())
                # Persist refreshed credentials with a fresh absolute expiry
                new_expiry_at = (
                    creds.expiry.timestamp()
                    if creds.expiry
                    else time.time() + 3600
                )
                updated = {
                    "access_token":  creds.token,
                    "refresh_token": creds.refresh_token or self._token.get("refresh_token"),
                    "token_type":    "Bearer",
                    "email":         self._token.get("email", ""),
                    "expiry_at":     new_expiry_at,
                }
                self._save_token(updated)
                print("[GDrive] Token refreshed successfully")
            elif creds.expired and not creds.refresh_token:
                # No refresh token — user must reauthorize
                print("[GDrive] Token expired and no refresh_token — reauth required")
                self._token = None
                self._send("GDRIVE_ERR,AUTH_REQUIRED\n")
                return False
            return True
        except Exception as exc:
            print(f"[GDrive] Token refresh failed: {exc}")
            # Treat refresh failure as auth-required so the user is prompted to
            # reauthorize rather than silently using a stale expired token.
            self._token = None
            self._send("GDRIVE_ERR,AUTH_REQUIRED\n")
            return False

    def _build_service(self):
        """Build an authenticated Google Drive v3 service. Returns None on failure."""
        if not DRIVE_AVAILABLE or not GOOGLE_AUTH_AVAILABLE or not self._token:
            return None
        try:
            if not self._maybe_refresh_token():
                # _maybe_refresh_token already emitted GDRIVE_ERR,AUTH_REQUIRED
                return None
            # Re-read from self._token in case refresh updated it
            expiry_at = self._token.get("expiry_at")
            expiry = (
                datetime.fromtimestamp(expiry_at, tz=timezone.utc)
                if expiry_at
                else None
            )
            creds = Credentials(
                token=self._token.get("access_token"),
                refresh_token=self._token.get("refresh_token"),
                token_uri=_TOKEN_URL,
                client_id=self._client_id,
                client_secret=self._client_secret,
                scopes=[_DRIVE_SCOPE],
                expiry=expiry,
            )
            return build("drive", "v3", credentials=creds, cache_discovery=False)
        except Exception as exc:
            print(f"[GDrive] Build service failed: {exc}")
            return None

    # ── Folder management ──────────────────────────────────────────────────────

    def set_credentials(self, client_id: str, client_secret: str) -> None:
        """
        Write OAuth client credentials to ~/.pi-tx/gdrive_client_secrets.json
        and reload them into memory so a GDRIVE_AUTH_URL request can follow
        immediately without restarting the daemon.

        Responds GDRIVE_CREDS_OK on success, GDRIVE_ERR,WRITE_FAILED on failure.
        Credentials travel only over the local LAN (same security boundary as all
        other Pi TX commands).
        """
        if not client_id or not client_secret:
            self._send("GDRIVE_ERR,WRITE_FAILED\n")
            return
        try:
            _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            payload = json.dumps({
                "installed": {
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "redirect_uris": ["urn:ietf:wg:oauth:2.0:oob"],
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": _TOKEN_URL,
                }
            }, indent=2)
            tmp = _CREDS_FILE.with_suffix(".tmp")
            tmp.write_text(payload)
            tmp.chmod(0o600)
            tmp.rename(_CREDS_FILE)
            # Reload into memory so get_auth_url() works immediately
            self._client_id = client_id
            self._client_secret = client_secret
            self._send("GDRIVE_CREDS_OK\n")
            print(f"[GDrive] Credentials saved to {_CREDS_FILE}")
        except Exception as exc:
            print(f"[GDrive] set_credentials failed: {exc}")
            self._send("GDRIVE_ERR,WRITE_FAILED\n")

    def set_folder(self, name: str) -> None:
        """Set the root Drive folder name. Invalidates the cached folder ID."""
        self._folder_name = name.strip() or "Pi TX Photos"
        self._folder_id   = None
        self._save_settings()
        folder_safe = self._folder_name.replace(",", " ")
        self._send(f"GDRIVE_FOLDER_SET,{folder_safe}\n")

    def _get_existing_drive_names(self, drive, folder_id: str) -> set:
        """
        Return the set of file names already present in a Drive folder (excludes trashed).
        Used to make uploads idempotent: partial-retry runs skip files already in Drive.
        """
        names      = set()
        page_token = None
        while True:
            kwargs: dict = {
                "q": (
                    f"'{folder_id}' in parents"
                    " and mimeType!='application/vnd.google-apps.folder'"
                    " and trashed=false"
                ),
                "fields":   "nextPageToken, files(name)",
                "pageSize": 1000,
            }
            if page_token:
                kwargs["pageToken"] = page_token
            result     = drive.files().list(**kwargs).execute()
            for f in result.get("files", []):
                names.add(f["name"])
            page_token = result.get("nextPageToken")
            if not page_token:
                break
        return names

    def _get_or_create_folder(self, drive, parent_id: str | None, name: str) -> str:
        """Find or create a Drive folder by name. Returns its file ID."""
        q = f"name='{name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
        if parent_id:
            q += f" and '{parent_id}' in parents"
        res = drive.files().list(q=q, fields="files(id)", spaces="drive").execute()
        files = res.get("files", [])
        if files:
            return files[0]["id"]
        meta: dict = {"name": name, "mimeType": "application/vnd.google-apps.folder"}
        if parent_id:
            meta["parents"] = [parent_id]
        folder = drive.files().create(body=meta, fields="id").execute()
        return folder["id"]

    # ── Status ─────────────────────────────────────────────────────────────────

    def get_status(self) -> None:
        """Emit GDRIVE_STATUS,<state>,<folder>,<total_uploaded>"""
        if not self.is_authed:
            state = "unauth"
        elif self._busy:
            state = "busy"
        elif self._last_error:
            state = "error"
        else:
            state = "ok"
        folder = self._folder_name.replace(",", " ")
        self._send(f"GDRIVE_STATUS,{state},{folder},{self._total_uploaded}\n")
        # If authed, also resend email so app can display it
        if self.is_authed:
            self._send(f"GDRIVE_AUTH_OK,{self._email or 'unknown'}\n")

    # ── Upload ─────────────────────────────────────────────────────────────────

    def upload_files(
        self,
        local_paths: list,
        date_str: str,
        progress_cb: Callable[[int, int], None],
    ) -> "tuple[int, int] | None":
        """
        Synchronous upload of local_paths to Drive.
        Call from a background thread so the HTTP server stays responsive.

        Returns:
          (successful_count, total_attempted) on completion.
          None if another upload was already running (GDRIVE_ERR,BUSY emitted).

        successful_count counts only files that were *actually* written to Drive.
        Files that are missing locally or fail to upload are NOT counted.
        """
        # Atomic busy guard — only one upload at a time.
        if not self._upload_lock.acquire(blocking=False):
            self._send("GDRIVE_ERR,BUSY\n")
            return None

        try:
            self._busy = True

            if not self.is_authed:
                self._send("GDRIVE_ERR,NOT_AUTHED\n")
                return (0, len(local_paths))
            if not DRIVE_AVAILABLE or not GOOGLE_AUTH_AVAILABLE:
                self._send("GDRIVE_ERR,GOOGLE_LIBS_NOT_INSTALLED\n")
                return (0, len(local_paths))

            drive = self._build_service()
            if not drive:
                self._send("GDRIVE_ERR,BUILD_SERVICE_FAILED\n")
                return (0, len(local_paths))

            try:
                # Ensure root folder and date subfolder
                if not self._folder_id:
                    self._folder_id = self._get_or_create_folder(drive, None, self._folder_name)
                date_folder_id = self._get_or_create_folder(drive, self._folder_id, date_str)
            except Exception as exc:
                err = str(exc).replace(",", ";")
                self._send(f"GDRIVE_ERR,{err}\n")
                return (0, len(local_paths))

            done       = 0
            successful = 0
            total      = len(local_paths)
            progress_cb(0, total)

            # Per-DCIM-subfolder caches so files with the same basename from different
            # camera folders (e.g. 100CANON/IMG001.JPG vs 101CANON/IMG001.JPG) are
            # never confused with each other.  Drive layout mirrors the camera:
            #   <Pi TX Photos>/<2026-08-16>/<100CANON>/IMG001.JPG
            dcim_folder_ids:     dict[str, str] = {}   # subfolder_name → Drive folder ID
            existing_by_dcim:    dict[str, set] = {}   # subfolder_name → set of names

            def _dcim_folder_id(subfolder_name: str) -> str:
                """Get (or create) the Drive subfolder for one camera DCIM directory."""
                if subfolder_name not in dcim_folder_ids:
                    dcim_folder_ids[subfolder_name] = self._get_or_create_folder(
                        drive, date_folder_id, subfolder_name
                    )
                return dcim_folder_ids[subfolder_name]

            def _existing_in(subfolder_name: str, subfolder_id: str) -> set:
                """Return the set of filenames already in a Drive subfolder (lazy-fetched)."""
                if subfolder_name not in existing_by_dcim:
                    try:
                        existing_by_dcim[subfolder_name] = self._get_existing_drive_names(
                            drive, subfolder_id
                        )
                        n = len(existing_by_dcim[subfolder_name])
                        if n:
                            print(f"[GDrive] {n} file(s) already in {subfolder_name}/ — will skip")
                    except Exception as exc:
                        print(f"[GDrive] Could not pre-fetch {subfolder_name}/: {exc}")
                        existing_by_dcim[subfolder_name] = set()
                return existing_by_dcim[subfolder_name]

            for path in local_paths:
                path = Path(path)
                if not path.exists():
                    print(f"[GDrive] Skipping missing file: {path}")
                    done += 1
                    progress_cb(done, total)
                    continue

                name           = path.name
                # Use the immediate parent directory name as the DCIM subfolder key
                # (e.g. "100CANON", "101CANON").  This is the component of the camera
                # path that distinguishes files with the same basename.
                dcim_subfolder = path.parent.name
                try:
                    sfolder_id = _dcim_folder_id(dcim_subfolder)
                except Exception as exc:
                    print(f"[GDrive] Could not create subfolder {dcim_subfolder}: {exc}")
                    done += 1
                    progress_cb(done, total)
                    continue

                existing = _existing_in(dcim_subfolder, sfolder_id)

                # Idempotency: (dcim_subfolder, name) is the unique identity.
                # Same basename in a *different* subfolder is a different file.
                if name in existing:
                    print(f"[GDrive] {dcim_subfolder}/{name} already in Drive — counting as uploaded")
                    self._total_uploaded += 1
                    successful += 1
                    done += 1
                    progress_cb(done, total)
                    continue

                ext      = path.suffix.lower()
                mimetype = _IMAGE_MIMETYPES.get(ext, "application/octet-stream")

                try:
                    file_meta = {
                        "name":    name,
                        "parents": [sfolder_id],
                    }
                    media = MediaFileUpload(str(path), mimetype=mimetype, resumable=True)
                    drive.files().create(
                        body=file_meta,
                        media_body=media,
                        fields="id",
                    ).execute()
                    self._total_uploaded += 1
                    successful += 1
                    existing.add(name)   # prevent double-upload within the same batch
                    print(f"[GDrive] Uploaded {dcim_subfolder}/{name}")
                except Exception as exc:
                    print(f"[GDrive] Upload {dcim_subfolder}/{name} failed: {exc}")

                done += 1
                progress_cb(done, total)

            self._save_settings()
            return (successful, total)

        finally:
            self._busy = False
            self._upload_lock.release()
