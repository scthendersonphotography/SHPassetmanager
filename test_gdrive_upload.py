"""
Tests for gdrive_upload.py — token expiry and refresh behaviour.

These tests mock the google.oauth2.credentials.Credentials class so they run
without installing google-auth or a real network connection.
"""

import json
import os
import stat
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_uploader(sent_lines, config_dir):
    """Return a GDriveUploader wired to a temp config dir and a sent-lines collector."""
    import gdrive_upload as gu

    gu._CONFIG_DIR   = config_dir
    gu._TOKEN_FILE   = config_dir / "gdrive_token.json"
    gu._SETTINGS_FILE = config_dir / "gdrive_settings.json"
    gu._CREDS_FILE   = config_dir / "gdrive_client_secrets.json"

    def capture(msg):
        sent_lines.append(msg.strip())

    # Patch env vars so _load_credentials sets dummy client creds
    with patch.dict(os.environ, {"GDRIVE_CLIENT_ID": "test-id", "GDRIVE_CLIENT_SECRET": "test-secret"}):
        # Reload module so _CONFIG_DIR changes take effect for module-level constants
        uploader = gu.GDriveUploader(capture)
    return uploader


def _future_expiry(seconds=3600):
    """Epoch seconds `seconds` in the future."""
    return time.time() + seconds


def _past_expiry(seconds=60):
    """Epoch seconds `seconds` in the past."""
    return time.time() - seconds


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

class TestSaveTokenStoresAbsoluteExpiry(unittest.TestCase):
    """_save_token must convert expires_in → expiry_at."""

    def test_expires_in_converted_to_absolute(self):
        with tempfile.TemporaryDirectory() as td:
            sent = []
            uploader = _make_uploader(sent, Path(td))
            before = time.time()
            uploader._save_token({
                "access_token":  "acc",
                "refresh_token": "ref",
                "expires_in":    3600,
                "email":         "u@example.com",
            })
            after = time.time()
            stored = json.loads((Path(td) / "gdrive_token.json").read_text())
            self.assertIn("expiry_at", stored)
            self.assertGreaterEqual(stored["expiry_at"], before + 3600)
            self.assertLessEqual(stored["expiry_at"],   after  + 3600)

    def test_existing_expiry_at_not_overwritten(self):
        """If expiry_at is already present, it must not be recalculated."""
        with tempfile.TemporaryDirectory() as td:
            sent = []
            uploader = _make_uploader(sent, Path(td))
            fixed_expiry = 9_999_999_999.0
            uploader._save_token({
                "access_token": "acc",
                "expires_in":   3600,
                "expiry_at":    fixed_expiry,
            })
            stored = json.loads((Path(td) / "gdrive_token.json").read_text())
            self.assertEqual(stored["expiry_at"], fixed_expiry)

    def test_token_file_permissions_0600(self):
        with tempfile.TemporaryDirectory() as td:
            sent = []
            uploader = _make_uploader(sent, Path(td))
            uploader._save_token({"access_token": "acc"})
            tok_file = Path(td) / "gdrive_token.json"
            mode = stat.S_IMODE(tok_file.stat().st_mode)
            self.assertEqual(oct(mode), "0o600")


class TestMaybeRefreshToken(unittest.TestCase):
    """_maybe_refresh_token must refresh when expiry_at is in the past."""

    def _fake_credentials(self, expired: bool, has_refresh: bool = True):
        """Return a mock Credentials object."""
        mock_creds = MagicMock()
        mock_creds.expired     = expired
        mock_creds.refresh_token = "ref" if has_refresh else None
        mock_creds.token       = "new_access"
        mock_creds.expiry      = datetime.fromtimestamp(time.time() + 3600, tz=timezone.utc)
        mock_creds.refresh     = MagicMock()   # no-op by default
        return mock_creds

    def test_valid_token_not_refreshed(self):
        """A token with a future expiry_at must not trigger a refresh call."""
        with tempfile.TemporaryDirectory() as td:
            sent = []
            uploader = _make_uploader(sent, Path(td))
            uploader._token = {
                "access_token":  "acc",
                "refresh_token": "ref",
                "expiry_at":     _future_expiry(),
            }
            mock_creds = self._fake_credentials(expired=False)
            with patch("gdrive_upload.GOOGLE_AUTH_AVAILABLE", True), \
                 patch("gdrive_upload.REQUESTS_AVAILABLE",    True), \
                 patch("gdrive_upload.Credentials", return_value=mock_creds):
                result = uploader._maybe_refresh_token()
            mock_creds.refresh.assert_not_called()
            self.assertTrue(result)

    def test_expired_token_triggers_refresh(self):
        """A token with a past expiry_at must call creds.refresh()."""
        with tempfile.TemporaryDirectory() as td:
            sent = []
            uploader = _make_uploader(sent, Path(td))
            uploader._token = {
                "access_token":  "old_access",
                "refresh_token": "ref",
                "email":         "u@example.com",
                "expiry_at":     _past_expiry(),
            }
            mock_creds = self._fake_credentials(expired=True, has_refresh=True)
            mock_google_request = MagicMock()
            with patch("gdrive_upload.GOOGLE_AUTH_AVAILABLE", True), \
                 patch("gdrive_upload.REQUESTS_AVAILABLE",    True), \
                 patch("gdrive_upload.Credentials", return_value=mock_creds), \
                 patch("gdrive_upload.GoogleRequest", return_value=mock_google_request):
                result = uploader._maybe_refresh_token()
            mock_creds.refresh.assert_called_once_with(mock_google_request)
            self.assertTrue(result)
            # Persisted token should now hold the refreshed access token
            stored = json.loads((Path(td) / "gdrive_token.json").read_text())
            self.assertEqual(stored["access_token"], "new_access")
            # expiry_at must have been updated
            self.assertIn("expiry_at", stored)

    def test_expired_token_no_refresh_token_emits_auth_required(self):
        """When the token is expired and no refresh_token is present, emit AUTH_REQUIRED."""
        with tempfile.TemporaryDirectory() as td:
            sent = []
            uploader = _make_uploader(sent, Path(td))
            uploader._token = {
                "access_token": "old",
                "expiry_at":    _past_expiry(),
                # No refresh_token key
            }
            mock_creds = self._fake_credentials(expired=True, has_refresh=False)
            with patch("gdrive_upload.GOOGLE_AUTH_AVAILABLE", True), \
                 patch("gdrive_upload.REQUESTS_AVAILABLE",    True), \
                 patch("gdrive_upload.Credentials", return_value=mock_creds):
                result = uploader._maybe_refresh_token()
            self.assertFalse(result)
            self.assertIsNone(uploader._token)
            self.assertIn("GDRIVE_ERR,AUTH_REQUIRED", sent)

    def test_refresh_exception_emits_auth_required(self):
        """A network error during refresh must emit AUTH_REQUIRED, not silently continue."""
        with tempfile.TemporaryDirectory() as td:
            sent = []
            uploader = _make_uploader(sent, Path(td))
            uploader._token = {
                "access_token":  "old",
                "refresh_token": "ref",
                "expiry_at":     _past_expiry(),
            }
            mock_creds = self._fake_credentials(expired=True, has_refresh=True)
            mock_creds.refresh.side_effect = Exception("network error")
            mock_google_request = MagicMock()
            with patch("gdrive_upload.GOOGLE_AUTH_AVAILABLE", True), \
                 patch("gdrive_upload.REQUESTS_AVAILABLE",    True), \
                 patch("gdrive_upload.Credentials", return_value=mock_creds), \
                 patch("gdrive_upload.GoogleRequest", return_value=mock_google_request):
                result = uploader._maybe_refresh_token()
            self.assertFalse(result)
            self.assertIsNone(uploader._token)
            self.assertIn("GDRIVE_ERR,AUTH_REQUIRED", sent)

    def test_no_google_auth_lib_returns_best_effort(self):
        """Without google-auth installed, treat token as valid if access_token present."""
        with tempfile.TemporaryDirectory() as td:
            sent = []
            uploader = _make_uploader(sent, Path(td))
            uploader._token = {"access_token": "acc"}
            with patch("gdrive_upload.GOOGLE_AUTH_AVAILABLE", False):
                result = uploader._maybe_refresh_token()
            self.assertTrue(result)
            self.assertEqual(sent, [])   # no error emitted


class TestUploadFilesAtomicBusyGuard(unittest.TestCase):
    """upload_files must return None (and emit GDRIVE_ERR,BUSY) if already locked."""

    def test_concurrent_upload_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            sent = []
            uploader = _make_uploader(sent, Path(td))
            # Manually hold the lock to simulate an in-progress upload
            uploader._upload_lock.acquire()
            try:
                result = uploader.upload_files([], "2026-08-16", lambda d, t: None)
            finally:
                uploader._upload_lock.release()
            self.assertIsNone(result)
            self.assertIn("GDRIVE_ERR,BUSY", sent)


class TestDcimSubfolderCollisionSafety(unittest.TestCase):
    """
    Files from different DCIM folders that share a basename must go to separate
    Drive subfolders and be deduplicated independently.
    """

    def _make_local_files(self, td: Path, camera_paths: list[str]) -> list[Path]:
        """
        Create fake local staging files that mirror the camera path structure.
        Returns a list of Path objects ready to pass to upload_files.
        """
        import gdrive_upload as gu
        local_paths = []
        for cam_path in camera_paths:
            local = Path(td) / "staging" / cam_path.lstrip("/")
            local.parent.mkdir(parents=True, exist_ok=True)
            local.write_bytes(b"\xff\xd8\xff")  # minimal JPEG header
            local_paths.append(local)
        return local_paths

    def _make_drive_mock(self, existing_by_subfolder: dict[str, list[str]]):
        """
        Build a mock Drive service whose files().list() returns per-subfolder
        existing files, and whose files().create() records uploads.
        """
        uploaded = []

        def list_side_effect(**kwargs):
            q: str = kwargs.get("q", "")
            # Extract folder_id from query "'{id}' in parents and ..."
            folder_id = q.split("'")[1] if "'" in q else ""
            names = existing_by_subfolder.get(folder_id, [])
            result_mock = MagicMock()
            result_mock.execute.return_value = {"files": [{"name": n} for n in names]}
            return result_mock

        def create_side_effect(body=None, media_body=None, fields=None):
            uploaded.append((body.get("parents", [None])[0], body.get("name")))
            result_mock = MagicMock()
            result_mock.execute.return_value = {"id": f"fake-{body['name']}"}
            return result_mock

        files_mock = MagicMock()
        files_mock.list.side_effect   = list_side_effect
        files_mock.create.side_effect = create_side_effect
        drive_mock = MagicMock()
        drive_mock.files.return_value = files_mock

        # folders: list() must return the folder-exists check too — reuse list_side_effect
        # But _get_or_create_folder also calls files().list() with a mimeType filter;
        # we'll just assign each folder a predictable ID via the create path.
        folder_create_ids: dict[str, str] = {}
        _orig_list = list_side_effect

        def smart_list(**kwargs):
            q: str = kwargs.get("q", "")
            if "application/vnd.google-apps.folder" in q:
                # Return empty so _get_or_create_folder always creates the folder
                result_mock = MagicMock()
                result_mock.execute.return_value = {"files": []}
                return result_mock
            return _orig_list(**kwargs)

        def smart_create(body=None, media_body=None, fields=None):
            if body and body.get("mimeType") == "application/vnd.google-apps.folder":
                folder_id = f"folder-{body['name']}"
                folder_create_ids[body["name"]] = folder_id
                # Register its expected existing files
                if body["name"] in existing_by_subfolder:
                    existing_by_subfolder[folder_id] = existing_by_subfolder[body["name"]]
                result_mock = MagicMock()
                result_mock.execute.return_value = {"id": folder_id}
                return result_mock
            return create_side_effect(body=body, media_body=media_body, fields=fields)

        files_mock.list.side_effect   = smart_list
        files_mock.create.side_effect = smart_create

        return drive_mock, uploaded, folder_create_ids

    def test_same_basename_different_dcim_folder_not_skipped(self):
        """
        IMG001.JPG from 100CANON/ and IMG001.JPG from 101CANON/ must be uploaded
        independently — the second must NOT be treated as a duplicate of the first.
        """
        with tempfile.TemporaryDirectory() as td:
            sent = []
            uploader = _make_uploader(sent, Path(td))

            cam_paths = [
                "/store_00010001/DCIM/100CANON/IMG001.JPG",
                "/store_00010001/DCIM/101CANON/IMG001.JPG",
            ]
            local_paths = self._make_local_files(Path(td), cam_paths)

            drive_mock, uploaded, _ = self._make_drive_mock({})

            with patch("gdrive_upload.GOOGLE_AUTH_AVAILABLE", True), \
                 patch("gdrive_upload.DRIVE_AVAILABLE", True), \
                 patch("gdrive_upload.Credentials", return_value=MagicMock(expired=False)), \
                 patch("gdrive_upload.build", return_value=drive_mock), \
                 patch("gdrive_upload.MediaFileUpload", return_value=MagicMock()):
                uploader._token = {"access_token": "tok", "expiry_at": _future_expiry()}
                result = uploader.upload_files(local_paths, "2026-08-16", lambda d, t: None)

            self.assertIsNotNone(result)
            successful, total = result
            self.assertEqual(total, 2)
            self.assertEqual(successful, 2, "Both files must be uploaded (different DCIM folders)")
            upload_names = [name for _, name in uploaded if name is not None]
            self.assertEqual(sorted(upload_names), ["IMG001.JPG", "IMG001.JPG"],
                             "Both IMG001.JPG files must appear as separate Drive uploads")

    def test_same_file_in_same_dcim_folder_skipped_on_retry(self):
        """
        On a retry, a file that was already uploaded to its DCIM subfolder must be
        counted as successful but NOT uploaded again.
        """
        with tempfile.TemporaryDirectory() as td:
            sent = []
            uploader = _make_uploader(sent, Path(td))

            cam_paths = ["/store_00010001/DCIM/100CANON/IMG001.JPG"]
            local_paths = self._make_local_files(Path(td), cam_paths)

            # Simulate Drive already containing IMG001.JPG in 100CANON
            drive_mock, uploaded, _ = self._make_drive_mock(
                {"100CANON": ["IMG001.JPG"]}
            )

            with patch("gdrive_upload.GOOGLE_AUTH_AVAILABLE", True), \
                 patch("gdrive_upload.DRIVE_AVAILABLE", True), \
                 patch("gdrive_upload.Credentials", return_value=MagicMock(expired=False)), \
                 patch("gdrive_upload.build", return_value=drive_mock), \
                 patch("gdrive_upload.MediaFileUpload", return_value=MagicMock()):
                uploader._token = {"access_token": "tok", "expiry_at": _future_expiry()}
                result = uploader.upload_files(local_paths, "2026-08-16", lambda d, t: None)

            self.assertIsNotNone(result)
            successful, total = result
            self.assertEqual(total, 1)
            self.assertEqual(successful, 1, "Already-uploaded file must be counted as success")
            file_uploads = [(fid, name) for fid, name in uploaded if fid and not fid.startswith("folder-")]
            self.assertEqual(len(file_uploads), 0, "File must NOT be uploaded again")


if __name__ == "__main__":
    unittest.main()
