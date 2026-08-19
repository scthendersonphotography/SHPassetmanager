"""
Basic USB webcam control via OpenCV / V4L2 for Pi TX — camMode 8.

Mirrors the CameraUSB public interface so TriggerEngine / TimeLapse /
command_parser work unchanged:
  start() / stop()          — background hot-plug detection thread
  connect() / disconnect()  — acquire / release /dev/video*
  trigger(shots, af_pre_ms) → bool
  push_connected_info()     — emit CAM_WEBCAM,CONNECTED + info lines

Captured JPEGs are written to the shared staging directory
(/tmp/pi-tx-cam — same as camera_download.STAGING_DIR) so the existing
Google Drive upload path picks them up without changes.

Completely optional — gracefully no-ops when cv2 (python3-opencv) is not
installed. Fixed-exposure only: resolution ("720p" | "1080p") is the single
configurable parameter.
"""

from __future__ import annotations

import glob
import os
import threading
import time
from datetime import datetime
from typing import Callable

from camera_download import STAGING_DIR

# ── Optional import ────────────────────────────────────────────────────────────
CV2_AVAILABLE = False
try:
    import cv2                     # python3-opencv (apt)
    CV2_AVAILABLE = True
except ImportError:
    pass

# Supported resolutions (alias → WxH). Actual capture may fall back to the
# closest mode the webcam supports — V4L2 negotiates silently.
RESOLUTIONS = {
    "720p":  (1280, 720),
    "1080p": (1920, 1080),
}
DEFAULT_RESOLUTION = "720p"

SETTINGS_KEY = "webcamResolution"

_HOTPLUG_POLL_S = 2.0
_BURST_GAP_S    = 0.2
# Frames to discard after opening / before capture so auto-exposure settles
_WARMUP_FRAMES  = 5


def _video_devices() -> list:
    return sorted(glob.glob("/dev/video*"))


class CameraWebcam:
    """Thread-safe V4L2 webcam driver (OpenCV)."""

    def __init__(self, send_response: Callable[[str], None], settings=None) -> None:
        self._send      = send_response
        self._settings  = settings
        self._lock      = threading.Lock()
        self._cap       = None    # cv2.VideoCapture when connected
        self._connected = False
        self._model     = "USB Webcam"
        self._running   = False
        self._thread: threading.Thread | None = None

    # ── Public properties ──────────────────────────────────────────────────────

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def model(self) -> str:
        return self._model

    @property
    def resolution(self) -> str:
        if self._settings is not None:
            alias = str(self._settings.get(SETTINGS_KEY, DEFAULT_RESOLUTION))
            if alias in RESOLUTIONS:
                return alias
        return DEFAULT_RESOLUTION

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start background hot-plug detection thread."""
        if not CV2_AVAILABLE:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._hotplug_loop, daemon=True, name="cam-webcam-hotplug"
        )
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=6.0)
        self.disconnect()

    # ── Connect / disconnect ───────────────────────────────────────────────────

    def connect(self) -> bool:
        """
        Open the first available /dev/video* device.
        Returns True on success; False when none found or cv2 missing.
        """
        if not CV2_AVAILABLE:
            self._send("CAM_ERR,OPENCV_NOT_INSTALLED\n")
            return False

        with self._lock:
            if self._connected and self._cap is not None:
                return True
            for dev in _video_devices():
                try:
                    cap = cv2.VideoCapture(dev, cv2.CAP_V4L2)
                    if not cap.isOpened():
                        cap.release()
                        continue
                    self._apply_resolution_locked(cap)
                    # Confirm the device actually delivers frames — some
                    # /dev/video* nodes are metadata-only and open fine but
                    # never produce an image.
                    ok, _ = cap.read()
                    if not ok:
                        cap.release()
                        continue
                    self._cap       = cap
                    self._connected = True
                    self._model     = f"Webcam ({dev})"
                    print(f"[CameraWebcam] Connected: {dev} @ {self.resolution}")
                    return True
                except Exception as exc:
                    print(f"[CameraWebcam] Open {dev} failed: {exc}")
            return False   # caller emits CAM_WEBCAM,NOT_FOUND

    def disconnect(self) -> None:
        with self._lock:
            self._release_locked()

    # ── Resolution ─────────────────────────────────────────────────────────────

    def set_resolution(self, alias: str) -> bool:
        """Persist + apply a new resolution alias. Returns True when valid."""
        alias = alias.strip()
        if alias not in RESOLUTIONS:
            return False
        if self._settings is not None:
            self._settings.set(SETTINGS_KEY, alias)
        with self._lock:
            if self._connected and self._cap is not None:
                try:
                    self._apply_resolution_locked(self._cap)
                except Exception as exc:
                    print(f"[CameraWebcam] Set resolution failed: {exc}")
                    return False
        return True

    # ── Trigger ────────────────────────────────────────────────────────────────

    def trigger(self, shots: int = 1, af_pre_ms: int = 0) -> bool:
        """
        Grab `shots` JPEG frames into the staging directory.
        af_pre_ms: optional settle delay before the first frame.
        Returns True if all frames were captured and written.
        """
        if not self._connected or self._cap is None:
            return False
        ok = True
        for i in range(max(1, shots)):
            if i > 0:
                time.sleep(_BURST_GAP_S)
            if af_pre_ms > 0 and i == 0:
                time.sleep(af_pre_ms / 1000.0)
            tmp_path, final_path = _staging_paths("webcam", i)
            try:
                with self._lock:
                    if self._cap is None:
                        return False
                    # Flush stale buffered frames so the capture reflects "now"
                    for _ in range(_WARMUP_FRAMES if i == 0 else 1):
                        self._cap.grab()
                    success, frame = self._cap.read()
                    if not success or frame is None:
                        raise RuntimeError("frame read failed")
                    if not cv2.imwrite(str(tmp_path), frame):
                        raise RuntimeError("imwrite failed")
                # Atomic publish: the uploader only sees fully-written JPEGs.
                os.replace(tmp_path, final_path)
            except Exception as exc:
                print(f"[CameraWebcam] Capture {i + 1} failed: {exc}")
                ok = False
        return ok

    # ── Push helpers ───────────────────────────────────────────────────────────

    def push_connected_info(self) -> None:
        """Emit CAM_WEBCAM,CONNECTED + model + current resolution."""
        self._send("CAM_WEBCAM,CONNECTED\n")
        self._send(f"CAM_WEBCAM_INFO,{self._model.replace(',', ' ')}\n")
        self._send(f"CAM_WEBCAM_RESOLUTION,{self.resolution}\n")

    # ── Hot-plug loop ──────────────────────────────────────────────────────────

    def _hotplug_loop(self) -> None:
        """
        Poll for webcam presence. Auto-connects only when webcam mode
        (camMode=8) is already active — mirrors the CameraUSB guard so the
        handle is never claimed while the user is in another mode.
        """
        while self._running:
            present = bool(_video_devices())

            if present and not self._connected:
                cam_mode = (
                    int(self._settings.get("camMode", 0))
                    if self._settings is not None
                    else 0
                )
                if cam_mode == 8:
                    if self.connect():
                        self.push_connected_info()

            elif not present and self._connected:
                with self._lock:
                    was = self._connected
                    self._release_locked()
                if was:
                    self._send("CAM_WEBCAM,DISCONNECTED\n")
                    print("[CameraWebcam] Webcam disconnected (hot-plug)")

            time.sleep(_HOTPLUG_POLL_S)

    # ── Internal ───────────────────────────────────────────────────────────────

    def _apply_resolution_locked(self, cap) -> None:
        w, h = RESOLUTIONS[self.resolution]
        cap.set(cv2.CAP_PROP_FRAME_WIDTH,  w)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)

    def _release_locked(self) -> None:
        if self._cap is not None:
            try:
                self._cap.release()
            except Exception:
                pass
            self._cap = None
        self._connected = False
        self._model     = "USB Webcam"


def _staging_paths(prefix: str, index: int):
    """
    Return (tmp_path, final_path) for a capture. The frame is written to
    tmp_path (a hidden .tmp subdir on the same filesystem) and the caller
    os.replace()s it to final_path, so the Drive uploader never observes a
    partially-written JPEG.
    """
    tmp_dir = STAGING_DIR / ".tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    name = f"{prefix}_{stamp}_{index}.jpg"
    return tmp_dir / name, STAGING_DIR / name
