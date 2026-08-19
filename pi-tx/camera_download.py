"""
Camera image downloader for Pi TX.

Downloads images from a connected USB camera via gphoto2, sharing the
CameraUSB._lock so both modules use the same process-level gphoto2 handle.

Supports:
  download_last_n(camera_usb, n, progress_cb)   — download last N images
  download_all_new(camera_usb, progress_cb)      — download images past cursor
  list_files(camera_usb)                         — list all image paths on camera
  advance_cursor(camera_files)                   — update the idempotency cursor
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Callable

GPHOTO2_AVAILABLE = False
try:
    import gphoto2 as gp
    GPHOTO2_AVAILABLE = True
except ImportError:
    pass

# Where downloaded images are staged before upload
STAGING_DIR = Path(tempfile.gettempdir()) / "pi-tx-cam"

# Cursor: path of the last camera file that was downloaded so `download_all_new`
# can pick up where it left off.
_CURSOR_FILE = Path.home() / ".pi-tx" / "gdrive_cursor.txt"

# Image file extensions to download (skip video, audio, THM thumbnails, etc.)
_IMAGE_EXTS = frozenset({
    ".jpg", ".jpeg",
    ".cr2", ".cr3",       # Canon RAW
    ".nef", ".nrw",       # Nikon RAW
    ".arw", ".srf", ".sr2",  # Sony RAW
    ".orf",               # Olympus RAW
    ".raf",               # Fujifilm RAW
    ".dng",               # Adobe DNG
    ".rw2",               # Panasonic RAW
    ".pef",               # Pentax RAW
    ".x3f",               # Sigma RAW
})


class CameraDownloader:
    """
    Downloads images from the USB camera into a local staging directory.
    Thread-safe: borrows CameraUSB._lock for each file so trigger events
    can interleave between downloads.
    """

    def __init__(self) -> None:
        STAGING_DIR.mkdir(parents=True, exist_ok=True)

    # ── Public API ─────────────────────────────────────────────────────────────

    def list_files(self, camera_usb) -> list[str]:
        """
        Return a sorted list of image file paths on the camera.
        Paths are camera-relative, e.g. /store_00010001/DCIM/100CANON/IMG001.CR3.
        Returns [] when camera is not connected or gphoto2 is unavailable.
        """
        if not GPHOTO2_AVAILABLE or not camera_usb.connected or camera_usb._camera is None:
            return []
        try:
            with camera_usb._lock:
                files: list[str] = []
                self._walk_folder(camera_usb._camera, "/", files)
                return sorted(files)
        except Exception as exc:
            print(f"[CamDownload] list_files failed: {exc}")
            return []

    def download_last_n(
        self,
        camera_usb,
        n: int,
        progress_cb: Callable[[int, int], None],
    ) -> "tuple[list[Path], int]":
        """
        Download the last `n` image files to the staging directory.
        progress_cb(done, total) is called after each file.

        Returns:
          (local_paths, target_count)
          - local_paths: Paths of files actually downloaded (only successes)
          - target_count: how many files were in the target slice
            (may be > len(local_paths) if some downloads failed)
        """
        all_files    = self.list_files(camera_usb)
        target       = all_files[-max(1, n):] if n < len(all_files) else all_files
        target_count = len(target)
        return self._download_files(camera_usb, target, progress_cb), target_count

    def download_all_new(
        self,
        camera_usb,
        progress_cb: Callable[[int, int], None],
    ) -> "tuple[list[Path], list[str], int]":
        """
        Download all images not yet downloaded, using a cursor for idempotency.

        Returns:
          (local_paths, all_camera_file_paths, target_count)
          - local_paths: paths of files that were *successfully* downloaded
          - all_camera_file_paths: the full list of files on the camera
          - target_count: how many files were in the new-since-cursor batch
            (may be > len(local_paths) if some downloads failed)

        The caller should compare len(local_paths) == target_count before
        advancing the cursor; if they differ, some downloads failed and the
        cursor must NOT advance so the next run can retry the missing files.
        """
        all_files = self.list_files(camera_usb)
        cursor    = self._read_cursor()
        if cursor and cursor in all_files:
            idx    = all_files.index(cursor) + 1
            target = all_files[idx:]
        else:
            target = all_files
        target_count = len(target)
        local = self._download_files(camera_usb, target, progress_cb)
        return local, all_files, target_count

    def advance_cursor(self, camera_files: list[str]) -> None:
        """Record the last camera path as the new download cursor."""
        if camera_files:
            _CURSOR_FILE.parent.mkdir(parents=True, exist_ok=True)
            try:
                _CURSOR_FILE.write_text(camera_files[-1])
            except Exception as exc:
                print(f"[CamDownload] advance_cursor failed: {exc}")

    # ── Internal ───────────────────────────────────────────────────────────────

    def _walk_folder(self, cam, folder: str, result: list[str]) -> None:
        """
        Recursively walk camera DCIM folders and collect image paths.
        Must be called with camera_usb._lock held.
        """
        try:
            # Recurse into subfolders first (depth-first)
            sub_folders = cam.folder_list_folders(folder)
            for name, _info in sub_folders:
                sub = f"{folder.rstrip('/')}/{name}"
                self._walk_folder(cam, sub, result)
        except Exception:
            pass

        try:
            files = cam.folder_list_files(folder)
            for name, _info in files:
                ext = os.path.splitext(name)[1].lower()
                if ext in _IMAGE_EXTS:
                    result.append(f"{folder.rstrip('/')}/{name}")
        except Exception:
            pass

    def _download_files(
        self,
        camera_usb,
        cam_paths: list[str],
        progress_cb: Callable[[int, int], None],
    ) -> list[Path]:
        """
        Download the given list of camera paths to STAGING_DIR.
        Holds the camera lock for each individual file, then releases between
        files so trigger events can still fire during a long download.
        """
        if not GPHOTO2_AVAILABLE:
            return []
        total   = len(cam_paths)
        results = []
        for i, cam_path in enumerate(cam_paths):
            folder = os.path.dirname(cam_path) or "/"
            name   = os.path.basename(cam_path)

            # Mirror the full camera path under STAGING_DIR so that files with the
            # same basename from different DCIM folders (e.g. 100CANON/IMG001.JPG
            # vs 101CANON/IMG001.JPG) never overwrite each other.
            rel  = cam_path.lstrip("/")   # "store_00010001/DCIM/100CANON/IMG001.JPG"
            dest = STAGING_DIR / rel
            dest.parent.mkdir(parents=True, exist_ok=True)

            try:
                with camera_usb._lock:
                    if not camera_usb.connected or camera_usb._camera is None:
                        print("[CamDownload] Camera disconnected during download")
                        break
                    cam_file = camera_usb._camera.file_get(
                        folder, name, gp.GP_FILE_TYPE_NORMAL
                    )
                    cam_file.save(str(dest))
                results.append(dest)
                print(f"[CamDownload] Downloaded {cam_path} → {dest}")
            except Exception as exc:
                print(f"[CamDownload] Failed to download {cam_path}: {exc}")

            progress_cb(i + 1, total)

        return results

    def _read_cursor(self) -> str:
        """Read the last-downloaded cursor, or '' if absent."""
        try:
            return _CURSOR_FILE.read_text().strip()
        except Exception:
            return ""
