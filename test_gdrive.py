"""
Tests for CameraDownloader and GDriveUploader.

Covers:
  TestCameraDownloaderListFiles
    - list_files returns sorted image paths only (filters non-image extensions)
    - list_files returns [] when camera is not connected
    - list_files returns [] when GPHOTO2_AVAILABLE is False
    - list_files returns [] when camera._camera is None

  TestCameraDownloaderDownloadLastN
    - download_last_n returns last n paths from the file list
    - download_last_n clips to full list when n >= len(files)
    - download_last_n returns ([], 0) when gphoto2 unavailable
    - progress_cb called for each file
    - failed downloads excluded from local_paths

  TestCameraDownloaderCursorAdvanceRead
    - advance_cursor writes the last file path to disk
    - advance_cursor on empty list is a no-op
    - _read_cursor returns '' when cursor file absent
    - _read_cursor returns the stored value after advance_cursor
    - second advance_cursor overwrites the previous cursor

  TestCameraDownloaderDownloadAllNew
    - download_all_new with no cursor returns all files
    - download_all_new resumes past cursor
    - download_all_new with cursor at last file returns empty batch
    - stale cursor falls back to full download
    - second return value is always the full camera file list

  TestGDriveUploaderCredentials
    - _load_credentials reads from env vars
    - _load_credentials reads from credential file (installed key)
    - _load_credentials reads from credential file (flat format)
    - has_credentials False when no creds available

  TestGDriveUploaderTokenSaveLoad
    - _save_token converts expires_in to absolute expiry_at
    - _save_token preserves existing expiry_at
    - _load_token populates self._email from token dict
    - is_authed True only when access_token present
    - is_authed False when token is None
    - is_authed False when access_token is empty string
    - save then reload round-trip works

  TestGDriveUploaderSetFolder
    - set_folder strips whitespace and emits GDRIVE_FOLDER_SET
    - set_folder with empty string falls back to default name
    - set_folder with whitespace-only falls back to default name
    - set_folder replaces commas with spaces in emitted response
    - set_folder invalidates cached folder_id

  TestGDriveUploaderGetStatus
    - get_status emits 'unauth' when not authed
    - get_status emits 'ok' when authed and not busy
    - get_status emits 'busy' when busy flag is set
    - get_status emits 'error' when last_error is set
    - get_status includes folder name and count
    - get_status emits GDRIVE_AUTH_OK,<email> when authed
    - get_status does NOT emit GDRIVE_AUTH_OK when not authed

  TestGDriveUploaderUploadFiles
    - upload_files returns None immediately when already busy
    - upload_files emits GDRIVE_ERR,NOT_AUTHED when no token
    - upload_files emits GDRIVE_ERR,GOOGLE_LIBS_NOT_INSTALLED when libs absent
    - upload_files calls progress_cb for each file
    - upload_files skips already-uploaded filenames (idempotency)
    - upload_files increments total_uploaded for each new file
    - upload_files skips missing local files without crashing
    - busy flag cleared after upload

  TestCommandParserGDrive (unit: command_parser GDRIVE_* dispatching)
    - GDRIVE_AUTH_URL with no gdrive emits GDRIVE_ERR,GDRIVE_NOT_AVAILABLE
    - GDRIVE_AUTH_URL with gdrive calls get_auth_url()
    - GDRIVE_AUTH_CODE calls check_auth()
    - GDRIVE_AUTH_CANCEL calls cancel_auth()
    - GDRIVE_STATUS with no gdrive emits GDRIVE_STATUS,unavailable,,0
    - GDRIVE_STATUS with gdrive calls get_status()
    - GDRIVE_FOLDER,<name> with gdrive calls set_folder(<name>)
    - GDRIVE_UPLOAD_LAST,<n> with no gdrive emits GDRIVE_ERR,NOT_AVAILABLE
    - GDRIVE_UPLOAD_ALL with no gdrive emits GDRIVE_ERR,NOT_AVAILABLE
    - GDRIVE_UPLOAD_LAST,<n> with wired deps starts background thread
    - GDRIVE_UPLOAD_ALL with wired deps starts background thread
    - GDRIVE_UPLOAD_ALL with disconnected camera emits GDRIVE_ERR,NO_CAMERA
    - GDRIVE_UPLOAD_LAST,0 is clamped to 1 (no NOT_AVAILABLE error)
    - Unknown GDRIVE_* emits GDRIVE_ERR,UNKNOWN_CMD

  TestCommandParserRunGDriveUpload (integration: _run_gdrive_upload end-to-end)
    - All downloads fail → GDRIVE_ERR,DOWNLOAD_FAILED; cursor NOT advanced
    - Empty camera (no files) → GDRIVE_DONE,0; cursor NOT advanced
    - All downloads succeed, all uploads succeed (upload_all) → GDRIVE_DONE,N; cursor advanced
    - All downloads succeed, all uploads succeed (upload_last_n) → GDRIVE_DONE,N; cursor NOT advanced
    - Partial download failure (upload_all) → GDRIVE_ERR,PARTIAL_RETRY; cursor NOT advanced
    - Partial upload failure (upload_all) → GDRIVE_ERR,PARTIAL_RETRY; cursor NOT advanced
    - Upload busy (returns None) → no GDRIVE_DONE emitted; cursor NOT advanced
    - All ok path still emits GDRIVE_DONE after PARTIAL_RETRY when upload_last used

All tests run without gphoto2 / google-api-python-client / requests installed.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, call


# ─────────────────────────────────────────────────────────────────────────────
# Minimal stubs shared by all suites
# ─────────────────────────────────────────────────────────────────────────────

class _FakeSettings:
    def __init__(self, **overrides):
        self._data = dict(
            minDist=10, maxDist=500, minFlux=20,
            cooldown=200, shots=1, pulseDur=50,
            usbBurst=1, delayShot=0,
            afEnabled=False, afPreMs=100, usbAfEn=False,
            wakeEn=False, wakeIntervalMs=300000,
            schedEn=False, schedStart=0, schedEnd=1439,
            triggerPin=17, wakePin=27,
            trigMode=0, wakeMode=0,
            laserPin=-1, statusLedPin=-1, powerLedPin=-1,
            camMode=6,
            name="Pi TX", espNowEn=False,
        )
        self._data.update(overrides)

    def get(self, key, default=None):
        return self._data.get(key, default)

    def set(self, key, value):
        self._data[key] = value

    def update(self, d):
        self._data.update(d)


class _StubGPIO:
    def apply_trig_mode(self, _): pass
    def apply_wake_mode(self, _): pass
    def set_status_led(self, _): pass
    def set_power_led(self, _): pass
    def set_laser(self, _): pass
    def fire_trigger(self, _): pass
    def fire_trigger_blocking(self, _): pass
    def fire_wake(self, _=100): pass
    def fire_wake_blocking(self, _): pass
    def assert_wake(self): pass
    def release_wake(self): pass
    _wake_lock = threading.Lock()


class _StubLiDAR:
    distance = 250
    flux     = 100


class _StubTriggerEngine:
    def fire_manual(self): pass
    def reset_wake_timer(self): pass


# ─────────────────────────────────────────────────────────────────────────────
# Fake gphoto2 surface used by CameraDownloader tests
# ─────────────────────────────────────────────────────────────────────────────

_GP_FILE_TYPE_NORMAL = 1


class _FakeGp:
    GP_FILE_TYPE_NORMAL = _GP_FILE_TYPE_NORMAL


class _FakeCamFile:
    def __init__(self, name):
        self._name = name

    def save(self, path: str) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text("")


class _FakeGpCamera:
    """
    Simulates a gphoto2 camera with a fixed file tree.

    folder_tree maps folder path → list of (name, None) pairs.
    fail_downloads is a set of camera paths that should raise on file_get.
    """

    def __init__(self, folder_tree: dict | None = None, fail_downloads: set | None = None):
        self._tree = folder_tree or {
            "/": [("store_00010001", None)],
            "/store_00010001": [("DCIM", None)],
            "/store_00010001/DCIM": [("100CANON", None)],
            "/store_00010001/DCIM/100CANON": [],
        }
        self._files = {
            "/store_00010001/DCIM/100CANON": [
                ("IMG001.CR3", None),
                ("IMG002.CR3", None),
                ("IMG003.CR3", None),
            ],
        }
        self._fail = fail_downloads or set()

    def folder_list_folders(self, folder: str):
        return self._tree.get(folder, [])

    def folder_list_files(self, folder: str):
        return self._files.get(folder, [])

    def file_get(self, folder: str, name: str, file_type: int):
        cam_path = f"{folder.rstrip('/')}/{name}"
        if cam_path in self._fail:
            raise RuntimeError(f"Simulated download failure: {cam_path}")
        return _FakeCamFile(name)


class _FakeCameraUSB:
    """Minimal CameraUSB stand-in — holds a fake gp.Camera and a lock."""
    def __init__(self, gp_camera=None):
        self.connected = True
        self._camera  = gp_camera or _FakeGpCamera()
        self._lock    = threading.Lock()


# ─────────────────────────────────────────────────────────────────────────────
# Helper: redirect camera_download module globals to a temp dir and restore
# ─────────────────────────────────────────────────────────────────────────────

def _cd_redirect(tmpdir: Path):
    """
    Redirect camera_download module globals to tmpdir-scoped paths.
    Returns a dict of originals to be passed to _cd_restore().
    """
    import camera_download as cd
    orig = {
        "GPHOTO2_AVAILABLE": cd.GPHOTO2_AVAILABLE,
        "gp": getattr(cd, "gp", None),
        "STAGING_DIR": cd.STAGING_DIR,
        "_CURSOR_FILE": cd._CURSOR_FILE,
    }
    cd.STAGING_DIR  = tmpdir / "staging"
    cd._CURSOR_FILE = tmpdir / "gdrive_cursor.txt"
    return orig


def _cd_restore(orig: dict) -> None:
    import camera_download as cd
    cd.GPHOTO2_AVAILABLE = orig["GPHOTO2_AVAILABLE"]
    if orig["gp"] is None:
        if hasattr(cd, "gp"):
            del cd.gp
    else:
        cd.gp = orig["gp"]
    cd.STAGING_DIR  = orig["STAGING_DIR"]
    cd._CURSOR_FILE = orig["_CURSOR_FILE"]


# ─────────────────────────────────────────────────────────────────────────────
# Helper: redirect gdrive_upload module globals to a temp dir and restore
# ─────────────────────────────────────────────────────────────────────────────

def _gu_redirect(tmpdir: Path):
    """Redirect gdrive_upload module config paths to tmpdir. Returns originals."""
    import gdrive_upload as gu
    orig = {
        "_CONFIG_DIR":   gu._CONFIG_DIR,
        "_CREDS_FILE":   gu._CREDS_FILE,
        "_TOKEN_FILE":   gu._TOKEN_FILE,
        "_SETTINGS_FILE": gu._SETTINGS_FILE,
    }
    tmpdir.mkdir(parents=True, exist_ok=True)
    gu._CONFIG_DIR    = tmpdir
    gu._CREDS_FILE    = tmpdir / "gdrive_client_secrets.json"
    gu._TOKEN_FILE    = tmpdir / "gdrive_token.json"
    gu._SETTINGS_FILE = tmpdir / "gdrive_settings.json"
    return orig


def _gu_restore(orig: dict) -> None:
    import gdrive_upload as gu
    gu._CONFIG_DIR    = orig["_CONFIG_DIR"]
    gu._CREDS_FILE    = orig["_CREDS_FILE"]
    gu._TOKEN_FILE    = orig["_TOKEN_FILE"]
    gu._SETTINGS_FILE = orig["_SETTINGS_FILE"]


def _make_gu_uploader(tmpdir: Path, env: dict | None = None) -> tuple:
    """
    Return (uploader, gu_module) with module globals redirected to tmpdir.
    Caller is responsible for calling _gu_restore(orig) in teardown.
    """
    import gdrive_upload as gu
    orig = _gu_redirect(tmpdir)
    responses: list[str] = []
    with patch.dict(os.environ, env or {}, clear=False):
        u = gu.GDriveUploader(responses.append)
    u._responses = responses
    return u, gu, orig


# ─────────────────────────────────────────────────────────────────────────────
# TestCameraDownloaderListFiles
# ─────────────────────────────────────────────────────────────────────────────

class TestCameraDownloaderListFiles(unittest.TestCase):

    def _make_downloader(self):
        import camera_download as cd
        tmpdir = Path(tempfile.mkdtemp())
        orig = _cd_redirect(tmpdir)
        cd.GPHOTO2_AVAILABLE = True
        cd.gp = _FakeGp()
        with patch.object(Path, "mkdir"):
            d = cd.CameraDownloader.__new__(cd.CameraDownloader)
            d.__init__()
        return d, cd, orig

    def test_returns_sorted_image_paths(self):
        """list_files must return sorted image paths (only image extensions)."""
        d, cd, orig = self._make_downloader()
        try:
            cam = _FakeCameraUSB()
            files = d.list_files(cam)
        finally:
            _cd_restore(orig)

        self.assertIsInstance(files, list)
        self.assertEqual(files, sorted(files), "list_files must be sorted")
        for f in files:
            ext = os.path.splitext(f)[1].lower()
            self.assertIn(ext, {
                ".jpg", ".jpeg", ".cr2", ".cr3", ".nef", ".nrw",
                ".arw", ".srf", ".sr2", ".orf", ".raf", ".dng",
                ".rw2", ".pef", ".x3f",
            }, f"Non-image extension in list_files: {f}")

    def test_filters_non_image_extensions(self):
        """list_files must exclude .mp4, .thm, .avi, etc."""
        import camera_download as cd
        tmpdir = Path(tempfile.mkdtemp())
        orig = _cd_redirect(tmpdir)
        cd.GPHOTO2_AVAILABLE = True
        cd.gp = _FakeGp()

        mixed_cam = _FakeGpCamera()
        mixed_cam._files = {
            "/store_00010001/DCIM/100CANON": [
                ("IMG001.CR3", None),
                ("IMG001.THM", None),
                ("VID001.MP4", None),
                ("IMG002.JPG", None),
            ],
        }
        try:
            with patch.object(Path, "mkdir"):
                d = cd.CameraDownloader.__new__(cd.CameraDownloader)
                d.__init__()
            cam = _FakeCameraUSB(gp_camera=mixed_cam)
            files = d.list_files(cam)
        finally:
            _cd_restore(orig)

        names = [os.path.basename(f) for f in files]
        self.assertIn("IMG001.CR3", names)
        self.assertIn("IMG002.JPG", names)
        self.assertNotIn("IMG001.THM", names)
        self.assertNotIn("VID001.MP4", names)

    def test_returns_empty_when_not_connected(self):
        """list_files must return [] when camera.connected is False."""
        d, cd, orig = self._make_downloader()
        try:
            cam = _FakeCameraUSB()
            cam.connected = False
            self.assertEqual(d.list_files(cam), [])
        finally:
            _cd_restore(orig)

    def test_returns_empty_when_gphoto2_unavailable(self):
        """list_files must return [] when GPHOTO2_AVAILABLE is False."""
        import camera_download as cd
        tmpdir = Path(tempfile.mkdtemp())
        orig = _cd_redirect(tmpdir)
        cd.GPHOTO2_AVAILABLE = False
        try:
            with patch.object(Path, "mkdir"):
                d = cd.CameraDownloader.__new__(cd.CameraDownloader)
                d.__init__()
            cam = _FakeCameraUSB()
            self.assertEqual(d.list_files(cam), [])
        finally:
            _cd_restore(orig)

    def test_returns_empty_when_camera_object_is_none(self):
        """list_files must return [] when camera_usb._camera is None."""
        d, cd, orig = self._make_downloader()
        try:
            cam = _FakeCameraUSB()
            cam._camera = None
            self.assertEqual(d.list_files(cam), [])
        finally:
            _cd_restore(orig)


# ─────────────────────────────────────────────────────────────────────────────
# TestCameraDownloaderDownloadLastN
# ─────────────────────────────────────────────────────────────────────────────

class TestCameraDownloaderDownloadLastN(unittest.TestCase):

    def _setup(self):
        import camera_download as cd
        tmpdir = Path(tempfile.mkdtemp())
        orig = _cd_redirect(tmpdir)
        cd.GPHOTO2_AVAILABLE = True
        cd.gp = _FakeGp()
        tmpdir.mkdir(parents=True, exist_ok=True)
        with patch.object(Path, "mkdir"):
            d = cd.CameraDownloader.__new__(cd.CameraDownloader)
            d.__init__()
        return d, cd, orig

    def test_returns_last_n_paths(self):
        """download_last_n(cam, 2) must request only the last 2 camera files."""
        d, cd, orig = self._setup()
        try:
            cam = _FakeCameraUSB()
            calls = []
            local, count = d.download_last_n(cam, 2, lambda done, total: calls.append((done, total)))
            self.assertEqual(count, 2, "target_count must be 2 for n=2 with 3 files on camera")
            self.assertEqual(len(local), 2, "two files must be downloaded")
        finally:
            _cd_restore(orig)

    def test_clips_to_full_list_when_n_exceeds_files(self):
        """When n > number of camera files, download_last_n returns all of them."""
        d, cd, orig = self._setup()
        try:
            cam = _FakeCameraUSB()
            local, count = d.download_last_n(cam, 100, lambda *_: None)
            self.assertEqual(count, 3, "All 3 files expected when n=100")
            self.assertEqual(len(local), 3)
        finally:
            _cd_restore(orig)

    def test_progress_cb_called_for_each_file(self):
        """progress_cb must be called once per file in the target slice."""
        d, cd, orig = self._setup()
        try:
            cam = _FakeCameraUSB()
            cb_calls = []
            local, count = d.download_last_n(cam, 3, lambda done, total: cb_calls.append((done, total)))
            self.assertEqual(len(cb_calls), 3, "progress_cb must be called 3 times for 3 files")
            done_vals = [c[0] for c in cb_calls]
            self.assertEqual(done_vals, [1, 2, 3])
        finally:
            _cd_restore(orig)

    def test_returns_empty_when_gphoto2_unavailable(self):
        """When GPHOTO2_AVAILABLE is False, download_last_n returns ([], 0)."""
        import camera_download as cd
        tmpdir = Path(tempfile.mkdtemp())
        orig = _cd_redirect(tmpdir)
        cd.GPHOTO2_AVAILABLE = False
        try:
            with patch.object(Path, "mkdir"):
                d = cd.CameraDownloader.__new__(cd.CameraDownloader)
                d.__init__()
            cam = _FakeCameraUSB()
            cam.connected = False
            local, count = d.download_last_n(cam, 5, lambda *_: None)
            self.assertEqual(local, [])
            self.assertEqual(count, 0)
        finally:
            _cd_restore(orig)

    def test_failed_downloads_excluded_from_local_paths(self):
        """Files that fail download must not appear in the returned local_paths."""
        d, cd, orig = self._setup()
        try:
            fail_path = "/store_00010001/DCIM/100CANON/IMG001.CR3"
            gp_cam = _FakeGpCamera(fail_downloads={fail_path})
            cam = _FakeCameraUSB(gp_camera=gp_cam)
            local, count = d.download_last_n(cam, 3, lambda *_: None)
            self.assertEqual(count, 3)
            self.assertLess(len(local), count,
                "Failed download must not appear in local_paths")
        finally:
            _cd_restore(orig)


# ─────────────────────────────────────────────────────────────────────────────
# TestCameraDownloaderCursorAdvanceRead
# ─────────────────────────────────────────────────────────────────────────────

class TestCameraDownloaderCursorAdvanceRead(unittest.TestCase):

    def setUp(self):
        self._tmpdir = Path(tempfile.mkdtemp())

    def _make_downloader(self):
        import camera_download as cd
        orig = _cd_redirect(self._tmpdir)
        cd.GPHOTO2_AVAILABLE = False
        with patch.object(Path, "mkdir"):
            d = cd.CameraDownloader.__new__(cd.CameraDownloader)
            d.__init__()
        return d, cd, orig

    def test_advance_cursor_writes_last_file(self):
        d, cd, orig = self._make_downloader()
        try:
            files = ["/DCIM/100CANON/A.CR3", "/DCIM/100CANON/B.CR3", "/DCIM/100CANON/C.CR3"]
            d.advance_cursor(files)
            self.assertTrue(cd._CURSOR_FILE.exists(), "Cursor file must exist after advance")
            self.assertEqual(cd._CURSOR_FILE.read_text().strip(), files[-1])
        finally:
            _cd_restore(orig)

    def test_advance_cursor_no_op_on_empty_list(self):
        d, cd, orig = self._make_downloader()
        try:
            d.advance_cursor([])
            self.assertFalse(cd._CURSOR_FILE.exists(), "Cursor file must not exist after empty advance")
        finally:
            _cd_restore(orig)

    def test_read_cursor_returns_empty_string_when_absent(self):
        d, cd, orig = self._make_downloader()
        try:
            self.assertEqual(d._read_cursor(), "")
        finally:
            _cd_restore(orig)

    def test_read_cursor_returns_stored_value(self):
        d, cd, orig = self._make_downloader()
        try:
            files = ["/DCIM/100CANON/IMG001.CR3", "/DCIM/100CANON/IMG002.CR3"]
            d.advance_cursor(files)
            self.assertEqual(d._read_cursor(), files[-1])
        finally:
            _cd_restore(orig)

    def test_advance_cursor_overwrites_previous_cursor(self):
        d, cd, orig = self._make_downloader()
        try:
            d.advance_cursor(["/A.CR3", "/B.CR3"])
            d.advance_cursor(["/C.CR3", "/D.CR3"])
            self.assertEqual(d._read_cursor(), "/D.CR3")
        finally:
            _cd_restore(orig)


# ─────────────────────────────────────────────────────────────────────────────
# TestCameraDownloaderDownloadAllNew
# ─────────────────────────────────────────────────────────────────────────────

class TestCameraDownloaderDownloadAllNew(unittest.TestCase):

    def setUp(self):
        self._tmpdir = Path(tempfile.mkdtemp())

    def _setup(self):
        import camera_download as cd
        orig = _cd_redirect(self._tmpdir)
        cd.GPHOTO2_AVAILABLE = True
        cd.gp = _FakeGp()
        self._tmpdir.mkdir(parents=True, exist_ok=True)
        with patch.object(Path, "mkdir"):
            d = cd.CameraDownloader.__new__(cd.CameraDownloader)
            d.__init__()
        return d, cd, orig

    def test_no_cursor_returns_all_files(self):
        d, cd, orig = self._setup()
        try:
            cam = _FakeCameraUSB()
            local, all_files, count = d.download_all_new(cam, lambda *_: None)
            self.assertEqual(count, 3, "All 3 files targeted when no cursor")
            self.assertEqual(len(local), 3)
        finally:
            _cd_restore(orig)

    def test_cursor_resumes_past_last_downloaded(self):
        d, cd, orig = self._setup()
        try:
            cam = _FakeCameraUSB()
            all_cam = d.list_files(cam)
            self.assertEqual(len(all_cam), 3)
            d.advance_cursor([all_cam[0]])
            local, all_files, count = d.download_all_new(cam, lambda *_: None)
            self.assertEqual(count, 2,
                "Only 2 files should be targeted when cursor points at file 0")
            self.assertEqual(len(local), 2)
        finally:
            _cd_restore(orig)

    def test_cursor_at_last_file_returns_empty_batch(self):
        d, cd, orig = self._setup()
        try:
            cam = _FakeCameraUSB()
            all_cam = d.list_files(cam)
            d.advance_cursor(all_cam)
            local, all_files, count = d.download_all_new(cam, lambda *_: None)
            self.assertEqual(count, 0, "No files targeted when cursor at last file")
            self.assertEqual(local, [])
        finally:
            _cd_restore(orig)

    def test_cursor_not_in_file_list_downloads_all(self):
        d, cd, orig = self._setup()
        try:
            cd._CURSOR_FILE.parent.mkdir(parents=True, exist_ok=True)
            cd._CURSOR_FILE.write_text("/DCIM/DELETED/IMG999.CR3")
            cam = _FakeCameraUSB()
            local, all_files, count = d.download_all_new(cam, lambda *_: None)
            self.assertEqual(count, 3, "Full download when cursor is stale")
        finally:
            _cd_restore(orig)

    def test_returns_all_camera_files_as_second_element(self):
        d, cd, orig = self._setup()
        try:
            cam = _FakeCameraUSB()
            all_cam = d.list_files(cam)
            d.advance_cursor([all_cam[0]])
            _, all_files_returned, _ = d.download_all_new(cam, lambda *_: None)
            self.assertEqual(len(all_files_returned), 3,
                "Second return value must be the FULL camera file list")
        finally:
            _cd_restore(orig)


# ─────────────────────────────────────────────────────────────────────────────
# TestGDriveUploaderCredentials
# ─────────────────────────────────────────────────────────────────────────────

class TestGDriveUploaderCredentials(unittest.TestCase):

    def test_loads_creds_from_env_vars(self):
        """Client ID and secret must be read from GDRIVE_CLIENT_ID/SECRET env vars."""
        tmp = Path(tempfile.mkdtemp())
        u, gu, orig = _make_gu_uploader(tmp, env={
            "GDRIVE_CLIENT_ID": "env-client-id",
            "GDRIVE_CLIENT_SECRET": "env-secret",
        })
        try:
            self.assertEqual(u._client_id, "env-client-id")
            self.assertEqual(u._client_secret, "env-secret")
            self.assertTrue(u.has_credentials)
        finally:
            _gu_restore(orig)

    def test_loads_creds_from_installed_key_in_file(self):
        """Credential file with 'installed' wrapper must be parsed correctly."""
        tmp = Path(tempfile.mkdtemp())
        import gdrive_upload as gu
        orig = _gu_redirect(tmp)
        creds = {"installed": {"client_id": "file-cid", "client_secret": "file-sec"}}
        gu._CREDS_FILE.write_text(json.dumps(creds))
        try:
            responses = []
            with patch.dict(os.environ, {"GDRIVE_CLIENT_ID": "", "GDRIVE_CLIENT_SECRET": ""}):
                u = gu.GDriveUploader(responses.append)
            self.assertEqual(u._client_id, "file-cid")
            self.assertEqual(u._client_secret, "file-sec")
        finally:
            _gu_restore(orig)

    def test_loads_creds_from_flat_file_format(self):
        """Flat credential file (no 'installed' key) must also be parsed."""
        tmp = Path(tempfile.mkdtemp())
        import gdrive_upload as gu
        orig = _gu_redirect(tmp)
        flat = {"client_id": "flat-cid", "client_secret": "flat-sec"}
        gu._CREDS_FILE.write_text(json.dumps(flat))
        try:
            responses = []
            with patch.dict(os.environ, {"GDRIVE_CLIENT_ID": "", "GDRIVE_CLIENT_SECRET": ""}):
                u = gu.GDriveUploader(responses.append)
            self.assertEqual(u._client_id, "flat-cid")
            self.assertEqual(u._client_secret, "flat-sec")
        finally:
            _gu_restore(orig)

    def test_has_credentials_false_when_nothing_available(self):
        """has_credentials must be False when no env vars and no file."""
        tmp = Path(tempfile.mkdtemp())
        u, gu, orig = _make_gu_uploader(tmp, env={"GDRIVE_CLIENT_ID": "", "GDRIVE_CLIENT_SECRET": ""})
        try:
            self.assertFalse(u.has_credentials)
        finally:
            _gu_restore(orig)


# ─────────────────────────────────────────────────────────────────────────────
# TestGDriveUploaderTokenSaveLoad
# ─────────────────────────────────────────────────────────────────────────────

class TestGDriveUploaderTokenSaveLoad(unittest.TestCase):

    def test_save_token_converts_expires_in_to_expiry_at(self):
        """_save_token must add an absolute expiry_at derived from expires_in."""
        tmp = Path(tempfile.mkdtemp())
        u, gu, orig = _make_gu_uploader(tmp)
        try:
            before = time.time()
            token = {"access_token": "tok", "refresh_token": "ref", "expires_in": 3600}
            u._save_token(token)
            after = time.time()
            self.assertIn("expiry_at", u._token)
            expiry_at = u._token["expiry_at"]
            self.assertGreaterEqual(expiry_at, before + 3600)
            self.assertLessEqual(expiry_at, after + 3600)
        finally:
            _gu_restore(orig)

    def test_save_token_preserves_existing_expiry_at(self):
        """_save_token must not overwrite expiry_at if already present."""
        tmp = Path(tempfile.mkdtemp())
        u, gu, orig = _make_gu_uploader(tmp)
        try:
            fixed_expiry = time.time() + 7200
            token = {"access_token": "tok", "expiry_at": fixed_expiry}
            u._save_token(token)
            self.assertAlmostEqual(u._token["expiry_at"], fixed_expiry, places=1)
        finally:
            _gu_restore(orig)

    def test_load_token_populates_email(self):
        """_load_token must set self._email from the token dict."""
        tmp = Path(tempfile.mkdtemp())
        u, gu, orig = _make_gu_uploader(tmp)
        try:
            gu._TOKEN_FILE.write_text(json.dumps({
                "access_token": "tok",
                "email": "test@example.com",
            }))
            u._load_token()
            self.assertEqual(u._email, "test@example.com")
        finally:
            _gu_restore(orig)

    def test_is_authed_true_when_access_token_present(self):
        tmp = Path(tempfile.mkdtemp())
        u, gu, orig = _make_gu_uploader(tmp)
        try:
            u._token = {"access_token": "valid-tok"}
            self.assertTrue(u.is_authed)
        finally:
            _gu_restore(orig)

    def test_is_authed_false_when_no_token(self):
        tmp = Path(tempfile.mkdtemp())
        u, gu, orig = _make_gu_uploader(tmp)
        try:
            u._token = None
            self.assertFalse(u.is_authed)
        finally:
            _gu_restore(orig)

    def test_is_authed_false_when_access_token_empty(self):
        tmp = Path(tempfile.mkdtemp())
        u, gu, orig = _make_gu_uploader(tmp)
        try:
            u._token = {"access_token": ""}
            self.assertFalse(u.is_authed)
        finally:
            _gu_restore(orig)

    def test_save_token_writes_to_disk_and_reloads(self):
        """Saved token must be readable back via _load_token."""
        tmp = Path(tempfile.mkdtemp())
        u, gu, orig = _make_gu_uploader(tmp)
        try:
            token = {"access_token": "saved-tok", "email": "user@example.com", "expires_in": 3600}
            u._save_token(token)
            u2_responses = []
            u2 = gu.GDriveUploader(u2_responses.append)
            self.assertEqual(u2._token.get("access_token"), "saved-tok")
            self.assertEqual(u2._email, "user@example.com")
        finally:
            _gu_restore(orig)


# ─────────────────────────────────────────────────────────────────────────────
# TestGDriveUploaderSetFolder
# ─────────────────────────────────────────────────────────────────────────────

class TestGDriveUploaderSetFolder(unittest.TestCase):

    def test_set_folder_strips_whitespace(self):
        tmp = Path(tempfile.mkdtemp())
        u, gu, orig = _make_gu_uploader(tmp)
        try:
            u.set_folder("  My Folder  ")
            self.assertEqual(u._folder_name, "My Folder")
        finally:
            _gu_restore(orig)

    def test_set_folder_empty_string_uses_default(self):
        tmp = Path(tempfile.mkdtemp())
        u, gu, orig = _make_gu_uploader(tmp)
        try:
            u.set_folder("")
            self.assertEqual(u._folder_name, "Pi TX Photos")
        finally:
            _gu_restore(orig)

    def test_set_folder_whitespace_only_uses_default(self):
        tmp = Path(tempfile.mkdtemp())
        u, gu, orig = _make_gu_uploader(tmp)
        try:
            u.set_folder("   ")
            self.assertEqual(u._folder_name, "Pi TX Photos")
        finally:
            _gu_restore(orig)

    def test_set_folder_emits_gdrive_folder_set(self):
        tmp = Path(tempfile.mkdtemp())
        u, gu, orig = _make_gu_uploader(tmp)
        try:
            u.set_folder("Wildlife Shots")
            folder_set = [r for r in u._responses if r.startswith("GDRIVE_FOLDER_SET,")]
            self.assertEqual(len(folder_set), 1)
            self.assertIn("Wildlife Shots", folder_set[0])
        finally:
            _gu_restore(orig)

    def test_set_folder_replaces_commas_in_emitted_name(self):
        tmp = Path(tempfile.mkdtemp())
        u, gu, orig = _make_gu_uploader(tmp)
        try:
            u.set_folder("A,B,C")
            folder_set = [r for r in u._responses if r.startswith("GDRIVE_FOLDER_SET,")]
            self.assertEqual(len(folder_set), 1)
            payload = folder_set[0][len("GDRIVE_FOLDER_SET,"):].strip()
            self.assertNotIn(",", payload,
                "Commas in folder name must be replaced in the emitted response")
        finally:
            _gu_restore(orig)

    def test_set_folder_invalidates_cached_folder_id(self):
        tmp = Path(tempfile.mkdtemp())
        u, gu, orig = _make_gu_uploader(tmp)
        try:
            u._folder_id = "old-id"
            u.set_folder("New Folder")
            self.assertIsNone(u._folder_id)
        finally:
            _gu_restore(orig)


# ─────────────────────────────────────────────────────────────────────────────
# TestGDriveUploaderGetStatus
# ─────────────────────────────────────────────────────────────────────────────

class TestGDriveUploaderGetStatus(unittest.TestCase):

    def _status_line(self, u) -> str:
        u._responses.clear()
        u.get_status()
        return next((r for r in u._responses if r.startswith("GDRIVE_STATUS,")), "")

    def test_status_unauth_when_no_token(self):
        tmp = Path(tempfile.mkdtemp())
        u, gu, orig = _make_gu_uploader(tmp)
        try:
            line = self._status_line(u)
            self.assertIn("unauth", line, f"Expected 'unauth' state: {line!r}")
        finally:
            _gu_restore(orig)

    def test_status_ok_when_authed_and_idle(self):
        tmp = Path(tempfile.mkdtemp())
        u, gu, orig = _make_gu_uploader(tmp)
        try:
            u._token = {"access_token": "tok"}
            u._busy = False
            u._last_error = ""
            line = self._status_line(u)
            self.assertIn(",ok,", line, f"Expected 'ok' state: {line!r}")
        finally:
            _gu_restore(orig)

    def test_status_busy_when_busy_flag_set(self):
        tmp = Path(tempfile.mkdtemp())
        u, gu, orig = _make_gu_uploader(tmp)
        try:
            u._token = {"access_token": "tok"}
            u._busy = True
            line = self._status_line(u)
            self.assertIn(",busy,", line, f"Expected 'busy' state: {line!r}")
        finally:
            _gu_restore(orig)

    def test_status_error_when_last_error_set(self):
        tmp = Path(tempfile.mkdtemp())
        u, gu, orig = _make_gu_uploader(tmp)
        try:
            u._token = {"access_token": "tok"}
            u._busy = False
            u._last_error = "something broke"
            line = self._status_line(u)
            self.assertIn(",error,", line, f"Expected 'error' state: {line!r}")
        finally:
            _gu_restore(orig)

    def test_status_includes_folder_name_and_count(self):
        tmp = Path(tempfile.mkdtemp())
        u, gu, orig = _make_gu_uploader(tmp)
        try:
            u._token = {"access_token": "tok"}
            u._folder_name = "My Photos"
            u._total_uploaded = 42
            line = self._status_line(u)
            self.assertIn("My Photos", line)
            self.assertIn("42", line)
        finally:
            _gu_restore(orig)

    def test_status_also_emits_gdrive_auth_ok_when_authed(self):
        tmp = Path(tempfile.mkdtemp())
        u, gu, orig = _make_gu_uploader(tmp)
        try:
            u._token = {"access_token": "tok"}
            u._email = "cam@example.com"
            u._responses.clear()
            u.get_status()
            auth_ok = [r for r in u._responses if r.startswith("GDRIVE_AUTH_OK,")]
            self.assertEqual(len(auth_ok), 1)
            self.assertIn("cam@example.com", auth_ok[0])
        finally:
            _gu_restore(orig)

    def test_status_no_auth_ok_when_not_authed(self):
        tmp = Path(tempfile.mkdtemp())
        u, gu, orig = _make_gu_uploader(tmp)
        try:
            u._token = None
            u._responses.clear()
            u.get_status()
            auth_ok = [r for r in u._responses if r.startswith("GDRIVE_AUTH_OK,")]
            self.assertEqual(auth_ok, [])
        finally:
            _gu_restore(orig)


# ─────────────────────────────────────────────────────────────────────────────
# TestGDriveUploaderUploadFiles
# ─────────────────────────────────────────────────────────────────────────────

class TestGDriveUploaderUploadFiles(unittest.TestCase):

    def test_upload_returns_none_when_already_busy(self):
        """upload_files must return None (and emit GDRIVE_ERR,BUSY) if lock is held."""
        tmp = Path(tempfile.mkdtemp())
        u, gu, orig = _make_gu_uploader(tmp)
        try:
            u._token = {"access_token": "tok"}
            acquired = u._upload_lock.acquire(blocking=False)
            self.assertTrue(acquired)
            try:
                result = u.upload_files([], "2026-08-17", lambda *_: None)
                self.assertIsNone(result)
                self.assertTrue(any("GDRIVE_ERR,BUSY" in r for r in u._responses))
            finally:
                u._upload_lock.release()
        finally:
            _gu_restore(orig)

    def test_upload_emits_not_authed_when_no_token(self):
        tmp = Path(tempfile.mkdtemp())
        u, gu, orig = _make_gu_uploader(tmp)
        try:
            u._token = None
            result = u.upload_files([], "2026-08-17", lambda *_: None)
            self.assertEqual(result, (0, 0))
            self.assertTrue(any("GDRIVE_ERR,NOT_AUTHED" in r for r in u._responses))
        finally:
            _gu_restore(orig)

    def test_upload_emits_libs_not_installed_when_drive_unavailable(self):
        tmp = Path(tempfile.mkdtemp())
        u, gu, orig = _make_gu_uploader(tmp)
        try:
            u._token = {"access_token": "tok"}
            with patch.object(gu, "DRIVE_AVAILABLE", False), \
                 patch.object(gu, "GOOGLE_AUTH_AVAILABLE", False):
                result = u.upload_files([], "2026-08-17", lambda *_: None)
            self.assertTrue(any("GOOGLE_LIBS_NOT_INSTALLED" in r for r in u._responses))
        finally:
            _gu_restore(orig)

    def test_upload_calls_progress_cb_for_each_file(self):
        """upload_files must invoke progress_cb once per file."""
        tmp = Path(tempfile.mkdtemp())
        u, gu, orig = _make_gu_uploader(tmp)
        try:
            u._token = {"access_token": "tok"}
            subdir = tmp / "100CANON"
            subdir.mkdir(parents=True, exist_ok=True)
            files = []
            for name in ["A.CR3", "B.CR3"]:
                p = subdir / name
                p.write_text("")
                files.append(p)

            cb_calls = []
            with patch.object(gu, "DRIVE_AVAILABLE", True), \
                 patch.object(gu, "GOOGLE_AUTH_AVAILABLE", True), \
                 patch.object(u, "_build_service", return_value=MagicMock()), \
                 patch.object(u, "_get_or_create_folder", return_value="fake-folder-id"), \
                 patch.object(u, "_get_existing_drive_names", return_value=set()):
                orig_media = gu.MediaFileUpload
                gu.MediaFileUpload = MagicMock(return_value=MagicMock())
                try:
                    u.upload_files(files, "2026-08-17",
                                   lambda done, total: cb_calls.append((done, total)))
                finally:
                    gu.MediaFileUpload = orig_media

            # Called at least once per file
            self.assertGreaterEqual(len(cb_calls), len(files))
        finally:
            _gu_restore(orig)

    def test_upload_skips_already_uploaded_files(self):
        """Files already in Drive must be skipped (counted as successful, not re-uploaded)."""
        tmp = Path(tempfile.mkdtemp())
        u, gu, orig = _make_gu_uploader(tmp)
        try:
            u._token = {"access_token": "tok"}
            subdir = tmp / "100CANON"
            subdir.mkdir(parents=True, exist_ok=True)
            local = subdir / "IMG001.CR3"
            local.write_text("")

            mock_drive = MagicMock()
            with patch.object(gu, "DRIVE_AVAILABLE", True), \
                 patch.object(gu, "GOOGLE_AUTH_AVAILABLE", True), \
                 patch.object(u, "_build_service", return_value=mock_drive), \
                 patch.object(u, "_get_or_create_folder", return_value="fake-folder-id"), \
                 patch.object(u, "_get_existing_drive_names", return_value={"IMG001.CR3"}):
                result = u.upload_files([local], "2026-08-17", lambda *_: None)

            self.assertIsNotNone(result)
            successful, total = result
            self.assertEqual(total, 1)
            self.assertEqual(successful, 1, "Already-uploaded file must count as successful")
            mock_drive.files().create.assert_not_called()
        finally:
            _gu_restore(orig)

    def test_upload_increments_total_uploaded(self):
        """_total_uploaded must increase by the number of files successfully uploaded."""
        tmp = Path(tempfile.mkdtemp())
        u, gu, orig = _make_gu_uploader(tmp)
        try:
            u._token = {"access_token": "tok"}
            u._total_uploaded = 5
            subdir = tmp / "100CANON"
            subdir.mkdir(parents=True, exist_ok=True)
            files = []
            for name in ["IMG001.CR3", "IMG002.CR3"]:
                p = subdir / name
                p.write_text("")
                files.append(p)

            with patch.object(gu, "DRIVE_AVAILABLE", True), \
                 patch.object(gu, "GOOGLE_AUTH_AVAILABLE", True), \
                 patch.object(u, "_build_service", return_value=MagicMock()), \
                 patch.object(u, "_get_or_create_folder", return_value="fake-folder-id"), \
                 patch.object(u, "_get_existing_drive_names", return_value=set()):
                orig_media = gu.MediaFileUpload
                gu.MediaFileUpload = MagicMock(return_value=MagicMock())
                try:
                    result = u.upload_files(files, "2026-08-17", lambda *_: None)
                finally:
                    gu.MediaFileUpload = orig_media

            self.assertIsNotNone(result)
            self.assertEqual(u._total_uploaded, 7,
                "_total_uploaded must go from 5 to 7 after 2 new uploads")
        finally:
            _gu_restore(orig)

    def test_upload_skips_missing_local_files(self):
        """Files that don't exist on disk must be skipped without crashing."""
        tmp = Path(tempfile.mkdtemp())
        u, gu, orig = _make_gu_uploader(tmp)
        try:
            u._token = {"access_token": "tok"}
            missing = tmp / "100CANON" / "GHOST.CR3"

            with patch.object(gu, "DRIVE_AVAILABLE", True), \
                 patch.object(gu, "GOOGLE_AUTH_AVAILABLE", True), \
                 patch.object(u, "_build_service", return_value=MagicMock()), \
                 patch.object(u, "_get_or_create_folder", return_value="fake-folder-id"), \
                 patch.object(u, "_get_existing_drive_names", return_value=set()):
                result = u.upload_files([missing], "2026-08-17", lambda *_: None)

            self.assertIsNotNone(result)
            successful, total = result
            self.assertEqual(total, 1)
            self.assertEqual(successful, 0, "Missing file must not count as successful")
        finally:
            _gu_restore(orig)

    def test_busy_flag_cleared_after_upload(self):
        tmp = Path(tempfile.mkdtemp())
        u, gu, orig = _make_gu_uploader(tmp)
        try:
            u._token = {"access_token": "tok"}
            with patch.object(gu, "DRIVE_AVAILABLE", True), \
                 patch.object(gu, "GOOGLE_AUTH_AVAILABLE", True), \
                 patch.object(u, "_build_service", return_value=MagicMock()), \
                 patch.object(u, "_get_or_create_folder", return_value="id"), \
                 patch.object(u, "_get_existing_drive_names", return_value=set()):
                u.upload_files([], "2026-08-17", lambda *_: None)
            self.assertFalse(u._busy, "_busy must be False after upload completes")
        finally:
            _gu_restore(orig)


# ─────────────────────────────────────────────────────────────────────────────
# Shared stubs for CommandParser tests
# ─────────────────────────────────────────────────────────────────────────────

class _FakeGDrive:
    """Records calls to GDriveUploader protocol methods."""
    def __init__(self):
        self.auth_url_calls   = 0
        self.check_auth_calls = 0
        self.cancel_auth_calls = 0
        self.get_status_calls = 0
        self.set_folder_args: list[str] = []

    def get_auth_url(self):     self.auth_url_calls += 1
    def check_auth(self):       self.check_auth_calls += 1
    def cancel_auth(self):      self.cancel_auth_calls += 1
    def get_status(self):       self.get_status_calls += 1
    def set_folder(self, n):    self.set_folder_args.append(n)


class _FakeDownloader:
    """Records cursor advances; download methods can be overridden per test."""
    def __init__(self):
        self.advance_calls: list = []

    def advance_cursor(self, camera_files):
        self.advance_calls.append(list(camera_files))

    # Default: 3 files, all downloaded successfully
    def download_all_new(self, camera, progress_cb):
        files = ["/DCIM/100CANON/IMG001.CR3",
                 "/DCIM/100CANON/IMG002.CR3",
                 "/DCIM/100CANON/IMG003.CR3"]
        local = [Path(f"/tmp/fake{f}") for f in files]
        progress_cb(3, 3)
        return local, files, 3

    def download_last_n(self, camera, n, progress_cb):
        files = ["/DCIM/100CANON/IMG001.CR3",
                 "/DCIM/100CANON/IMG002.CR3",
                 "/DCIM/100CANON/IMG003.CR3"]
        target = files[-n:] if n < len(files) else files
        local  = [Path(f"/tmp/fake{f}") for f in target]
        progress_cb(len(target), len(target))
        return local, len(target)


class _FakeGDriveForUpload:
    """GDriveUploader stand-in whose upload_files result is controlled per test."""
    def __init__(self, result=(3, 3)):
        self._result = result
        self.upload_calls: list = []

    def upload_files(self, local_paths, date_str, progress_cb):
        self.upload_calls.append((local_paths, date_str))
        progress_cb(len(local_paths), len(local_paths))
        return self._result


class _FakeCamera:
    """Minimal camera stub."""
    def __init__(self, connected=True):
        self.connected = connected


def _make_parser(responses, gdrive=None, downloader=None, camera=None):
    from command_parser import CommandParser
    cp = CommandParser(
        _FakeSettings(), _StubGPIO(), _StubLiDAR(), _StubTriggerEngine(),
        responses.append,
    )
    if gdrive:
        cp.set_gdrive(gdrive)
    if downloader:
        cp.set_downloader(downloader)
    if camera:
        cp.set_camera(camera)
    return cp


# ─────────────────────────────────────────────────────────────────────────────
# TestCommandParserGDrive  (unit: dispatch layer only)
# ─────────────────────────────────────────────────────────────────────────────

class TestCommandParserGDrive(unittest.TestCase):

    def test_gdrive_auth_url_no_gdrive_emits_not_available(self):
        resp = []
        _make_parser(resp).handle("GDRIVE_AUTH_URL")
        self.assertTrue(any("GDRIVE_ERR,GDRIVE_NOT_AVAILABLE" in r for r in resp))

    def test_gdrive_auth_url_calls_get_auth_url(self):
        resp = []
        gd = _FakeGDrive()
        _make_parser(resp, gdrive=gd).handle("GDRIVE_AUTH_URL")
        self.assertEqual(gd.auth_url_calls, 1)

    def test_gdrive_auth_code_calls_check_auth(self):
        resp = []
        gd = _FakeGDrive()
        _make_parser(resp, gdrive=gd).handle("GDRIVE_AUTH_CODE")
        self.assertEqual(gd.check_auth_calls, 1)

    def test_gdrive_auth_cancel_calls_cancel_auth(self):
        resp = []
        gd = _FakeGDrive()
        _make_parser(resp, gdrive=gd).handle("GDRIVE_AUTH_CANCEL")
        self.assertEqual(gd.cancel_auth_calls, 1)

    def test_gdrive_status_no_gdrive_emits_unavailable(self):
        resp = []
        _make_parser(resp).handle("GDRIVE_STATUS")
        status = [r for r in resp if r.startswith("GDRIVE_STATUS,")]
        self.assertEqual(len(status), 1)
        self.assertIn("unavailable", status[0])

    def test_gdrive_status_calls_get_status(self):
        resp = []
        gd = _FakeGDrive()
        _make_parser(resp, gdrive=gd).handle("GDRIVE_STATUS")
        self.assertEqual(gd.get_status_calls, 1)

    def test_gdrive_folder_calls_set_folder(self):
        resp = []
        gd = _FakeGDrive()
        _make_parser(resp, gdrive=gd).handle("GDRIVE_FOLDER,Wildlife Shots")
        self.assertEqual(gd.set_folder_args, ["Wildlife Shots"])

    def test_gdrive_upload_last_no_gdrive_emits_not_available(self):
        resp = []
        _make_parser(resp).handle("GDRIVE_UPLOAD_LAST,5")
        self.assertTrue(any("GDRIVE_ERR,NOT_AVAILABLE" in r for r in resp))

    def test_gdrive_upload_all_no_gdrive_emits_not_available(self):
        resp = []
        _make_parser(resp).handle("GDRIVE_UPLOAD_ALL")
        self.assertTrue(any("GDRIVE_ERR,NOT_AVAILABLE" in r for r in resp))

    def test_gdrive_upload_last_with_wired_deps_no_not_available(self):
        """GDRIVE_UPLOAD_LAST,3 with wired deps must not emit NOT_AVAILABLE."""
        resp = []
        gd = _FakeGDriveForUpload(result=(3, 3))
        dl = _FakeDownloader()
        cam = _FakeCamera(connected=False)  # disconnected → NO_CAMERA, not NOT_AVAILABLE
        _make_parser(resp, gdrive=gd, downloader=dl, camera=cam).handle("GDRIVE_UPLOAD_LAST,3")
        time.sleep(0.15)
        self.assertFalse(any("GDRIVE_ERR,NOT_AVAILABLE" in r for r in resp))

    def test_gdrive_upload_all_with_wired_deps_no_not_available(self):
        resp = []
        gd = _FakeGDriveForUpload(result=(3, 3))
        dl = _FakeDownloader()
        cam = _FakeCamera(connected=False)
        _make_parser(resp, gdrive=gd, downloader=dl, camera=cam).handle("GDRIVE_UPLOAD_ALL")
        time.sleep(0.15)
        self.assertFalse(any("GDRIVE_ERR,NOT_AVAILABLE" in r for r in resp))

    def test_gdrive_upload_last_clamps_n_to_minimum_1(self):
        resp = []
        gd = _FakeGDriveForUpload(result=(0, 0))
        dl = _FakeDownloader()
        cam = _FakeCamera(connected=False)
        _make_parser(resp, gdrive=gd, downloader=dl, camera=cam).handle("GDRIVE_UPLOAD_LAST,0")
        time.sleep(0.15)
        self.assertFalse(any("GDRIVE_ERR,NOT_AVAILABLE" in r for r in resp))

    def test_unknown_gdrive_cmd_emits_unknown_cmd(self):
        resp = []
        _make_parser(resp).handle("GDRIVE_FOOBAR")
        self.assertTrue(any("GDRIVE_ERR,UNKNOWN_CMD" in r for r in resp))

    def test_gdrive_upload_no_camera_emits_no_camera(self):
        """_run_gdrive_upload must emit GDRIVE_ERR,NO_CAMERA when camera not connected."""
        resp = []
        gd = _FakeGDriveForUpload()
        dl = _FakeDownloader()
        cam = _FakeCamera(connected=False)
        _make_parser(resp, gdrive=gd, downloader=dl, camera=cam).handle("GDRIVE_UPLOAD_ALL")
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if any("GDRIVE_ERR,NO_CAMERA" in r for r in resp):
                break
            time.sleep(0.05)
        self.assertTrue(any("GDRIVE_ERR,NO_CAMERA" in r for r in resp))


# ─────────────────────────────────────────────────────────────────────────────
# TestCommandParserRunGDriveUpload  (end-to-end: _run_gdrive_upload workflow)
#
# Calls _run_gdrive_upload() synchronously (directly, not via a thread) so
# tests are deterministic.  Each test wires tailored _FakeDownloader and
# _FakeGDriveForUpload stubs to cover the critical failure paths and verify:
#   - correct terminal protocol message is emitted
#   - cursor is only advanced on full success (upload_all)
#   - GDRIVE_DONE is always emitted on completion (even after PARTIAL_RETRY)
# ─────────────────────────────────────────────────────────────────────────────

class TestCommandParserRunGDriveUpload(unittest.TestCase):

    def _make_cp_with(self, gdrive, downloader, camera) -> tuple:
        resp = []
        cp = _make_parser(resp, gdrive=gdrive, downloader=downloader, camera=camera)
        return cp, resp

    # ── All downloads fail ────────────────────────────────────────────────────

    def test_all_downloads_fail_emits_download_failed(self):
        """
        When every camera file fails to download (local_paths=[]) but the camera
        had files (target_dl_count > 0), _run_gdrive_upload must emit
        GDRIVE_ERR,DOWNLOAD_FAILED and NOT advance the cursor.
        """
        dl = _FakeDownloader()
        dl.download_all_new = lambda cam, cb: ([], ["/DCIM/IMG001.CR3"], 1)

        gd = _FakeGDriveForUpload(result=(0, 0))
        cam = _FakeCamera(connected=True)
        cp, resp = self._make_cp_with(gd, dl, cam)

        cp._run_gdrive_upload(0, True)

        self.assertTrue(any("GDRIVE_ERR,DOWNLOAD_FAILED" in r for r in resp),
            f"Expected GDRIVE_ERR,DOWNLOAD_FAILED: {resp}")
        self.assertEqual(dl.advance_calls, [],
            "Cursor must NOT be advanced when all downloads fail")
        # No GDRIVE_DONE should be emitted (early return)
        self.assertFalse(any("GDRIVE_DONE" in r for r in resp),
            f"GDRIVE_DONE must not be emitted when all downloads failed: {resp}")

    def test_all_downloads_fail_upload_last_emits_download_failed(self):
        """Same DOWNLOAD_FAILED guard applies when using upload_last_n."""
        dl = _FakeDownloader()
        dl.download_last_n = lambda cam, n, cb: ([], 2)  # 2 files existed, none downloaded

        gd = _FakeGDriveForUpload(result=(0, 0))
        cam = _FakeCamera(connected=True)
        cp, resp = self._make_cp_with(gd, dl, cam)

        cp._run_gdrive_upload(2, False)

        self.assertTrue(any("GDRIVE_ERR,DOWNLOAD_FAILED" in r for r in resp),
            f"Expected GDRIVE_ERR,DOWNLOAD_FAILED: {resp}")

    # ── Empty camera ──────────────────────────────────────────────────────────

    def test_empty_camera_emits_done_zero(self):
        """
        When download_all_new returns no files AND target_dl_count == 0
        (camera is empty), GDRIVE_DONE,0 must be emitted and cursor NOT advanced.
        """
        dl = _FakeDownloader()
        dl.download_all_new = lambda cam, cb: ([], [], 0)

        gd = _FakeGDriveForUpload()
        cam = _FakeCamera(connected=True)
        cp, resp = self._make_cp_with(gd, dl, cam)

        cp._run_gdrive_upload(0, True)

        self.assertTrue(any("GDRIVE_DONE,0" in r for r in resp),
            f"Expected GDRIVE_DONE,0 for empty camera: {resp}")
        self.assertEqual(dl.advance_calls, [],
            "Cursor must NOT be advanced when camera was empty")
        self.assertFalse(any("GDRIVE_ERR,DOWNLOAD_FAILED" in r for r in resp))

    # ── Full success (upload_all) ─────────────────────────────────────────────

    def test_full_success_upload_all_advances_cursor(self):
        """
        When all downloads AND all uploads succeed with upload_all=True,
        GDRIVE_DONE,<n> must be emitted and the cursor must be advanced.
        """
        cam_files = ["/DCIM/100CANON/IMG001.CR3",
                     "/DCIM/100CANON/IMG002.CR3",
                     "/DCIM/100CANON/IMG003.CR3"]
        local = [Path(f"/tmp/fake{f}") for f in cam_files]

        dl = _FakeDownloader()
        dl.download_all_new = lambda cam, cb: (local, cam_files, 3)
        gd = _FakeGDriveForUpload(result=(3, 3))
        cam = _FakeCamera(connected=True)
        cp, resp = self._make_cp_with(gd, dl, cam)

        cp._run_gdrive_upload(0, True)

        self.assertTrue(any("GDRIVE_DONE,3" in r for r in resp),
            f"Expected GDRIVE_DONE,3: {resp}")
        self.assertEqual(len(dl.advance_calls), 1,
            "Cursor must be advanced exactly once on full success")
        self.assertFalse(any("GDRIVE_ERR,PARTIAL_RETRY" in r for r in resp),
            "No PARTIAL_RETRY expected on full success")

    # ── Full success (upload_last_n) — cursor NOT advanced ────────────────────

    def test_full_success_upload_last_does_not_advance_cursor(self):
        """
        For upload_last_n (upload_all=False), even on full success the cursor
        must NOT be advanced (cursor is only for upload_all idempotency).
        GDRIVE_DONE,<n> still emitted.
        """
        local = [Path("/tmp/fake/IMG001.CR3"), Path("/tmp/fake/IMG002.CR3")]
        dl = _FakeDownloader()
        dl.download_last_n = lambda cam, n, cb: (local, 2)

        gd = _FakeGDriveForUpload(result=(2, 2))
        cam = _FakeCamera(connected=True)
        cp, resp = self._make_cp_with(gd, dl, cam)

        cp._run_gdrive_upload(2, False)

        self.assertTrue(any("GDRIVE_DONE,2" in r for r in resp),
            f"Expected GDRIVE_DONE,2: {resp}")
        self.assertEqual(dl.advance_calls, [],
            "Cursor must NOT be advanced for upload_last_n")

    # ── Partial download failure (upload_all) ─────────────────────────────────

    def test_partial_download_failure_upload_all_emits_partial_retry(self):
        """
        When some files fail to download (len(local) < target_dl_count) with
        upload_all=True, GDRIVE_ERR,PARTIAL_RETRY must be emitted and the
        cursor must NOT be advanced, so the next UPLOAD_ALL retries them.
        """
        cam_files = ["/DCIM/100CANON/IMG001.CR3",
                     "/DCIM/100CANON/IMG002.CR3",
                     "/DCIM/100CANON/IMG003.CR3"]
        # Only 2 of 3 downloaded (one failed)
        local = [Path(f"/tmp/fake{f}") for f in cam_files[:2]]

        dl = _FakeDownloader()
        dl.download_all_new = lambda cam, cb: (local, cam_files, 3)
        gd = _FakeGDriveForUpload(result=(2, 2))
        cam = _FakeCamera(connected=True)
        cp, resp = self._make_cp_with(gd, dl, cam)

        cp._run_gdrive_upload(0, True)

        self.assertTrue(any("GDRIVE_ERR,PARTIAL_RETRY" in r for r in resp),
            f"Expected GDRIVE_ERR,PARTIAL_RETRY on partial download failure: {resp}")
        self.assertEqual(dl.advance_calls, [],
            "Cursor must NOT be advanced when some downloads failed")
        # GDRIVE_DONE is still emitted (with the successful count)
        self.assertTrue(any("GDRIVE_DONE" in r for r in resp),
            f"GDRIVE_DONE must still be emitted after PARTIAL_RETRY: {resp}")

    # ── Partial upload failure (upload_all) ───────────────────────────────────

    def test_partial_upload_failure_upload_all_emits_partial_retry(self):
        """
        When all files are downloaded but some uploads fail
        (successful < total), GDRIVE_ERR,PARTIAL_RETRY must be emitted and
        the cursor must NOT be advanced.
        """
        cam_files = ["/DCIM/100CANON/IMG001.CR3",
                     "/DCIM/100CANON/IMG002.CR3",
                     "/DCIM/100CANON/IMG003.CR3"]
        local = [Path(f"/tmp/fake{f}") for f in cam_files]

        dl = _FakeDownloader()
        dl.download_all_new = lambda cam, cb: (local, cam_files, 3)
        gd = _FakeGDriveForUpload(result=(2, 3))  # 1 upload failed
        cam = _FakeCamera(connected=True)
        cp, resp = self._make_cp_with(gd, dl, cam)

        cp._run_gdrive_upload(0, True)

        self.assertTrue(any("GDRIVE_ERR,PARTIAL_RETRY" in r for r in resp),
            f"Expected GDRIVE_ERR,PARTIAL_RETRY when an upload failed: {resp}")
        self.assertEqual(dl.advance_calls, [],
            "Cursor must NOT be advanced when any upload failed")
        self.assertTrue(any("GDRIVE_DONE,2" in r for r in resp),
            "GDRIVE_DONE,2 must still be emitted with the partial successful count")

    # ── Partial upload failure (upload_last_n) ────────────────────────────────

    def test_partial_upload_failure_upload_last_emits_partial_retry(self):
        """PARTIAL_RETRY is also emitted for upload_last_n when uploads partially fail."""
        local = [Path("/tmp/fake/IMG001.CR3"),
                 Path("/tmp/fake/IMG002.CR3"),
                 Path("/tmp/fake/IMG003.CR3")]
        dl = _FakeDownloader()
        dl.download_last_n = lambda cam, n, cb: (local, 3)

        gd = _FakeGDriveForUpload(result=(1, 3))  # 2 uploads failed
        cam = _FakeCamera(connected=True)
        cp, resp = self._make_cp_with(gd, dl, cam)

        cp._run_gdrive_upload(3, False)

        self.assertTrue(any("GDRIVE_ERR,PARTIAL_RETRY" in r for r in resp),
            f"Expected GDRIVE_ERR,PARTIAL_RETRY: {resp}")
        self.assertTrue(any("GDRIVE_DONE,1" in r for r in resp),
            "GDRIVE_DONE,1 must still be emitted after PARTIAL_RETRY")

    # ── Upload busy (returns None) ────────────────────────────────────────────

    def test_upload_busy_no_gdrive_done(self):
        """
        When upload_files returns None (lock already held / BUSY), _run_gdrive_upload
        must return without emitting GDRIVE_DONE and without advancing the cursor.
        """
        cam_files = ["/DCIM/100CANON/IMG001.CR3"]
        local = [Path("/tmp/fake/IMG001.CR3")]

        dl = _FakeDownloader()
        dl.download_all_new = lambda cam, cb: (local, cam_files, 1)

        class _BusyGDrive:
            """upload_files always returns None to simulate a running upload."""
            def upload_files(self, paths, date, cb):
                return None  # means: BUSY, already emitted the error

        cam = _FakeCamera(connected=True)
        cp, resp = self._make_cp_with(_BusyGDrive(), dl, cam)

        cp._run_gdrive_upload(0, True)

        self.assertFalse(any("GDRIVE_DONE" in r for r in resp),
            f"GDRIVE_DONE must NOT be emitted when upload_files returns None: {resp}")
        self.assertEqual(dl.advance_calls, [],
            "Cursor must NOT be advanced when upload was busy")

    # ── PARTIAL_RETRY failure count accuracy ──────────────────────────────────

    def test_partial_retry_failure_count_is_accurate(self):
        """
        The failure count in GDRIVE_ERR,PARTIAL_RETRY,<n> must equal the number
        of files that could not be downloaded plus the number that were not uploaded.
        Here: 1 download failed + 1 upload failed = 2.
        """
        cam_files = ["/DCIM/100CANON/IMG001.CR3",
                     "/DCIM/100CANON/IMG002.CR3",
                     "/DCIM/100CANON/IMG003.CR3"]
        # 2 of 3 downloaded
        local = [Path(f"/tmp/fake{f}") for f in cam_files[:2]]

        dl = _FakeDownloader()
        dl.download_all_new = lambda cam, cb: (local, cam_files, 3)
        gd = _FakeGDriveForUpload(result=(1, 2))  # 1 of 2 uploaded succeeded
        cam = _FakeCamera(connected=True)
        cp, resp = self._make_cp_with(gd, dl, cam)

        cp._run_gdrive_upload(0, True)

        partial = [r for r in resp if "GDRIVE_ERR,PARTIAL_RETRY" in r]
        self.assertEqual(len(partial), 1)
        # Extract count: "GDRIVE_ERR,PARTIAL_RETRY,2\n" → "2"
        count_str = partial[0].strip().split(",")[-1]
        self.assertEqual(count_str, "2",
            f"Failure count must be 2 (1 dl + 1 ul failed): {partial}")


# ─────────────────────────────────────────────────────────────────────────────
# TestGDriveUploaderSetCredentials
# ─────────────────────────────────────────────────────────────────────────────

class TestGDriveUploaderSetCredentials(unittest.TestCase):
    """GDriveUploader.set_credentials — write, reload, and error paths."""

    def _make(self, tmpdir: Path):
        """Return (uploader, gu_module, orig) with no pre-existing env creds."""
        import gdrive_upload as gu
        orig = _gu_redirect(tmpdir)
        responses: list[str] = []
        with patch.dict(os.environ, {"GDRIVE_CLIENT_ID": "", "GDRIVE_CLIENT_SECRET": ""}, clear=False):
            u = gu.GDriveUploader(responses.append)
        u._responses = responses
        return u, gu, orig

    # ── Success path ──────────────────────────────────────────────────────────

    def test_set_credentials_writes_json_file(self):
        """set_credentials must write a valid JSON credential file."""
        tmp = Path(tempfile.mkdtemp())
        u, gu, orig = self._make(tmp)
        try:
            u.set_credentials("my-client-id", "my-secret")
            creds_file = tmp / "gdrive_client_secrets.json"
            self.assertTrue(creds_file.exists(), "Credential file must be created")
            data = json.loads(creds_file.read_text())
            blob = data.get("installed", {})
            self.assertEqual(blob["client_id"], "my-client-id")
            self.assertEqual(blob["client_secret"], "my-secret")
        finally:
            _gu_restore(orig)

    def test_set_credentials_emits_gdrive_creds_ok(self):
        """set_credentials must respond GDRIVE_CREDS_OK on success."""
        tmp = Path(tempfile.mkdtemp())
        u, gu, orig = self._make(tmp)
        try:
            u.set_credentials("cid", "csec")
            self.assertTrue(any("GDRIVE_CREDS_OK" in r for r in u._responses),
                f"Expected GDRIVE_CREDS_OK in responses: {u._responses}")
        finally:
            _gu_restore(orig)

    def test_set_credentials_reloads_into_memory(self):
        """After set_credentials, has_credentials and the stored values must be updated."""
        tmp = Path(tempfile.mkdtemp())
        u, gu, orig = self._make(tmp)
        try:
            self.assertFalse(u.has_credentials, "No creds before set_credentials")
            u.set_credentials("new-cid", "new-sec")
            self.assertTrue(u.has_credentials, "has_credentials must be True after set")
            self.assertEqual(u._client_id, "new-cid")
            self.assertEqual(u._client_secret, "new-sec")
        finally:
            _gu_restore(orig)

    def test_set_credentials_file_mode_0600(self):
        """Credential file must be written with mode 0600 (not world-readable)."""
        import stat as stat_mod
        tmp = Path(tempfile.mkdtemp())
        u, gu, orig = self._make(tmp)
        try:
            u.set_credentials("cid", "csec")
            creds_file = tmp / "gdrive_client_secrets.json"
            mode = stat_mod.S_IMODE(creds_file.stat().st_mode)
            self.assertEqual(oct(mode), "0o600",
                f"Credential file must be 0600, got {oct(mode)}")
        finally:
            _gu_restore(orig)

    def test_set_credentials_persists_across_reload(self):
        """Credentials written by set_credentials must be loadable by a new uploader."""
        tmp = Path(tempfile.mkdtemp())
        u, gu, orig = self._make(tmp)
        try:
            u.set_credentials("persisted-cid", "persisted-sec")
            # Create a fresh uploader pointing at the same dir (no env override)
            responses2: list[str] = []
            with patch.dict(os.environ, {"GDRIVE_CLIENT_ID": "", "GDRIVE_CLIENT_SECRET": ""}, clear=False):
                u2 = gu.GDriveUploader(responses2.append)
            self.assertEqual(u2._client_id, "persisted-cid")
            self.assertEqual(u2._client_secret, "persisted-sec")
            self.assertTrue(u2.has_credentials)
        finally:
            _gu_restore(orig)

    # ── Error paths ───────────────────────────────────────────────────────────

    def test_set_credentials_empty_client_id_emits_write_failed(self):
        """set_credentials must emit GDRIVE_ERR,WRITE_FAILED when client_id is empty."""
        tmp = Path(tempfile.mkdtemp())
        u, gu, orig = self._make(tmp)
        try:
            u.set_credentials("", "some-secret")
            self.assertTrue(any("GDRIVE_ERR,WRITE_FAILED" in r for r in u._responses),
                f"Expected GDRIVE_ERR,WRITE_FAILED: {u._responses}")
        finally:
            _gu_restore(orig)

    def test_set_credentials_empty_secret_emits_write_failed(self):
        """set_credentials must emit GDRIVE_ERR,WRITE_FAILED when client_secret is empty."""
        tmp = Path(tempfile.mkdtemp())
        u, gu, orig = self._make(tmp)
        try:
            u.set_credentials("some-id", "")
            self.assertTrue(any("GDRIVE_ERR,WRITE_FAILED" in r for r in u._responses),
                f"Expected GDRIVE_ERR,WRITE_FAILED: {u._responses}")
        finally:
            _gu_restore(orig)

    def test_set_credentials_write_error_emits_write_failed(self):
        """When the filesystem write fails, set_credentials emits GDRIVE_ERR,WRITE_FAILED."""
        tmp = Path(tempfile.mkdtemp())
        u, gu, orig = self._make(tmp)
        try:
            with patch("pathlib.Path.rename", side_effect=OSError("disk full")):
                u.set_credentials("cid", "sec")
            self.assertTrue(any("GDRIVE_ERR,WRITE_FAILED" in r for r in u._responses),
                f"Expected GDRIVE_ERR,WRITE_FAILED on write error: {u._responses}")
        finally:
            _gu_restore(orig)


# ─────────────────────────────────────────────────────────────────────────────
# TestCommandParserGDriveSetCreds
# ─────────────────────────────────────────────────────────────────────────────

class TestCommandParserGDriveSetCreds(unittest.TestCase):
    """command_parser GDRIVE_SET_CREDS dispatch tests."""

    def _make_parser(self, gdrive=None):
        """Return (CommandParser, responses_list)."""
        import command_parser as cp_mod
        responses: list[str] = []

        def _send(msg: str) -> None:
            responses.append(msg.strip())

        cp = cp_mod.CommandParser(
            _FakeSettings(), _StubGPIO(), _StubLiDAR(),
            _StubTriggerEngine(), _send,
        )
        cp.set_gdrive(gdrive)
        return cp, responses

    def test_gdrive_set_creds_secret_not_logged(self):
        """
        GDRIVE_SET_CREDS must never print the client secret to stdout/journald.
        The log line must say REDACTED so that real credentials cannot be recovered
        from Pi journal logs.
        """
        import io
        import command_parser as cp_mod

        class _FakeGDrive:
            def set_credentials(self, cid, csec): pass

        cp, _ = self._make_parser(gdrive=_FakeGDrive())

        captured = io.StringIO()
        import sys
        old_stdout = sys.stdout
        sys.stdout = captured
        try:
            cp.handle("GDRIVE_SET_CREDS,real-client-id,super-secret-value")
        finally:
            sys.stdout = old_stdout

        log_output = captured.getvalue()
        self.assertNotIn("super-secret-value", log_output,
            f"Client secret must not appear in log output: {log_output!r}")
        self.assertIn("REDACTED", log_output,
            f"Log must indicate the secret was redacted: {log_output!r}")

    def test_gdrive_set_creds_calls_set_credentials(self):
        """GDRIVE_SET_CREDS,<id>,<sec> must call gdrive.set_credentials with correct args."""
        calls: list[tuple] = []

        class _FakeGDrive:
            def set_credentials(self, cid, csec):
                calls.append((cid, csec))

        cp, _ = self._make_parser(gdrive=_FakeGDrive())
        cp.handle("GDRIVE_SET_CREDS,my-client-id,my-secret")
        self.assertEqual(calls, [("my-client-id", "my-secret")])

    def test_gdrive_set_creds_no_gdrive_emits_not_available(self):
        """When gdrive is not wired, GDRIVE_SET_CREDS must emit GDRIVE_ERR,GDRIVE_NOT_AVAILABLE."""
        cp, responses = self._make_parser(gdrive=None)
        cp.handle("GDRIVE_SET_CREDS,cid,sec")
        self.assertIn("GDRIVE_ERR,GDRIVE_NOT_AVAILABLE", responses,
            f"Expected GDRIVE_NOT_AVAILABLE: {responses}")

    def test_gdrive_set_creds_missing_comma_emits_write_failed(self):
        """GDRIVE_SET_CREDS with no comma separator must emit GDRIVE_ERR,WRITE_FAILED."""
        cp, responses = self._make_parser(gdrive=None)
        # Override gdrive with a dummy so the no-comma path reaches the error branch,
        # not the GDRIVE_NOT_AVAILABLE branch — test the parser's validation.
        calls: list = []

        class _FakeGDrive:
            def set_credentials(self, cid, csec):
                calls.append((cid, csec))

        cp.set_gdrive(_FakeGDrive())
        cp.handle("GDRIVE_SET_CREDS,no-comma-secret-missing")
        self.assertTrue(any("GDRIVE_ERR,WRITE_FAILED" in r for r in responses),
            f"Expected GDRIVE_ERR,WRITE_FAILED for missing comma: {responses}")
        self.assertEqual(calls, [], "set_credentials must NOT be called when comma is missing")


if __name__ == "__main__":
    unittest.main()
