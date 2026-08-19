"""
Pi Camera (CSI ribbon) control via picamera2 for Pi TX — camMode 7.

Mirrors the CameraUSB public interface so TriggerEngine / TimeLapse /
command_parser work unchanged:
  start() / stop()          — lifecycle (no background thread needed; CSI
                              cameras are not hot-pluggable, so presence is
                              probed on connect())
  connect() / disconnect()  — acquire / release the camera
  trigger(shots, af_pre_ms) → bool
  push_connected_info()     — emit CAM_PI,CONNECTED + info lines

Captured JPEGs are written to the shared staging directory
(/tmp/pi-tx-cam — same as camera_download.STAGING_DIR) so the existing
Google Drive upload path picks them up without changes.

Completely optional — gracefully no-ops when picamera2 is not installed
(e.g. running the daemon on non-Pi hardware for development).

Resolution is configurable on all models ("1080p" or "4K").

Pi Camera 3 (Sony IMX708) additionally exposes:
  - AF mode (continuous / auto / manual), one-shot AF trigger, lens position
  - Exposure: auto (AEC) or manual shutter milliseconds
  - Gain: auto (AGC) or manual analogue gain (≈ ISO/100)
  - White balance: auto or preset
These are detected at connect() (model string contains "imx708") and are
silent no-ops on older camera modules.
"""

from __future__ import annotations

import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

from camera_download import STAGING_DIR

# ── Optional import ────────────────────────────────────────────────────────────
PICAMERA2_AVAILABLE = False
try:
    from picamera2 import Picamera2          # python3-picamera2 (apt)
    PICAMERA2_AVAILABLE = True
except ImportError:
    pass

# Supported still resolutions (alias → WxH)
RESOLUTIONS = {
    "1080p": (1920, 1080),
    "4K":    (3840, 2160),
}
DEFAULT_RESOLUTION = "1080p"

# Low-resolution preview stream dimensions (lores stream, always-on)
PREVIEW_SIZE = (640, 360)

# Settings key for the persisted resolution alias
SETTINGS_KEY = "piCamResolution"

# ── Pi Camera 3 control constants ─────────────────────────────────────────────
# libcamera AfModeEnum values (used as raw ints so libcamera import is optional)
AF_MODES = {"manual": 0, "auto": 1, "continuous": 2}
DEFAULT_AF_MODE = "continuous"

# libcamera AwbModeEnum values
AWB_PRESETS = {
    "auto":        0,
    "Tungsten":    2,
    "Fluorescent": 3,
    "Indoor":      4,
    "Daylight":    5,
    "Cloudy":      6,
}
DEFAULT_AWB = "auto"

LENS_POS_MIN, LENS_POS_MAX = 0.0, 10.0     # dioptres: 0 = infinity, 10 ≈ 10 cm
EXPOSURE_MS_MIN, EXPOSURE_MS_MAX = 1, 10000
GAIN_MIN, GAIN_MAX = 1.0, 16.0

# Settings keys for persisted Pi Camera 3 controls
KEY_AF_MODE  = "piCamAfMode"
KEY_LENS_POS = "piCamLensPos"
KEY_EXPOSURE = "piCamExposure"   # "auto" or integer milliseconds (as str)
KEY_GAIN     = "piCamGain"       # "auto" or float analogue gain (as str)
KEY_AWB      = "piCamAwb"

# Inter-shot gap for burst sequences — matches GPIO / USB burst gap
_BURST_GAP_S = 0.2


class CameraPi:
    """Thread-safe CSI Pi Camera driver (picamera2)."""

    def __init__(self, send_response: Callable[[str], None], settings=None) -> None:
        self._send      = send_response
        self._settings  = settings
        self._lock      = threading.Lock()
        self._picam     = None    # Picamera2 instance when connected
        self._connected = False
        self._model     = "Pi Camera"
        self._has_af    = False   # True on Pi Camera 3 (IMX708) — PDAF + full controls
        self._af_busy   = False   # one-shot AF cycle in flight
        # Separate tiny lock for the AF busy flag so concurrent triggers see
        # "in flight" immediately instead of queueing behind the camera lock
        # (which the AF worker holds for the whole cycle).
        self._af_flag_lock = threading.Lock()
        # ── Preview cache ──────────────────────────────────────────────────────
        # Filled by picamera2's post_callback (fires once per completed camera
        # request, in picamera2's own thread — no trigger lock involved).
        # get_preview_jpeg() returns the latest cached bytes instantly so the
        # HTTP handler never waits for a camera frame or holds any trigger lock.
        self._lores_available = False  # True once lores stream is configured
        self._preview_cache: "bytes | None" = None
        self._preview_cache_lock = threading.Lock()
        self._last_preview_encode_t: float = 0.0  # monotonic, throttle ~1 fps

    # ── Public properties ──────────────────────────────────────────────────────

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def model(self) -> str:
        return self._model

    @property
    def has_af(self) -> bool:
        """True when the connected camera is a Pi Camera 3 (PDAF + controls)."""
        return self._has_af

    @property
    def af_mode(self) -> str:
        return self._setting_str(KEY_AF_MODE, DEFAULT_AF_MODE, AF_MODES)

    @property
    def lens_position(self) -> float:
        try:
            v = float(self._settings.get(KEY_LENS_POS, 0.0)) if self._settings else 0.0
        except (TypeError, ValueError):
            v = 0.0
        return min(max(v, LENS_POS_MIN), LENS_POS_MAX)

    @property
    def exposure(self) -> str:
        """'auto' or manual shutter time in whole milliseconds (as str)."""
        raw = str(self._settings.get(KEY_EXPOSURE, "auto")) if self._settings else "auto"
        if raw == "auto":
            return "auto"
        try:
            return str(min(max(int(float(raw)), EXPOSURE_MS_MIN), EXPOSURE_MS_MAX))
        except (TypeError, ValueError):
            return "auto"

    @property
    def gain(self) -> str:
        """'auto' or manual analogue gain (as str)."""
        raw = str(self._settings.get(KEY_GAIN, "auto")) if self._settings else "auto"
        if raw == "auto":
            return "auto"
        try:
            return f"{min(max(float(raw), GAIN_MIN), GAIN_MAX):g}"
        except (TypeError, ValueError):
            return "auto"

    @property
    def awb(self) -> str:
        return self._setting_str(KEY_AWB, DEFAULT_AWB, AWB_PRESETS)

    def _setting_str(self, key: str, default: str, allowed: dict) -> str:
        if self._settings is not None:
            v = str(self._settings.get(key, default))
            if v in allowed:
                return v
        return default

    @property
    def resolution(self) -> str:
        """Current resolution alias ('1080p' | '4K')."""
        if self._settings is not None:
            alias = str(self._settings.get(SETTINGS_KEY, DEFAULT_RESOLUTION))
            if alias in RESOLUTIONS:
                return alias
        return DEFAULT_RESOLUTION

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    def start(self) -> None:
        """No background thread — CSI cameras are fixed; probe on connect()."""

    def stop(self) -> None:
        self.disconnect()

    # ── Connect / disconnect ───────────────────────────────────────────────────

    def connect(self) -> bool:
        """
        Acquire the CSI camera and configure a still pipeline.
        Returns True on success; False when no camera or picamera2 missing.
        """
        if not PICAMERA2_AVAILABLE:
            self._send("CAM_ERR,PICAMERA2_NOT_INSTALLED\n")
            return False

        with self._lock:
            if self._connected and self._picam is not None:
                return True
            try:
                if not Picamera2.global_camera_info():
                    return False   # caller emits CAM_PI,NOT_FOUND
                picam = Picamera2()
                self._configure_locked(picam)
                picam.start()
                # Give AGC/AWB a moment to settle before the first capture
                time.sleep(0.4)
                # Register preview callback BEFORE storing picam so get_preview_jpeg()
                # won't see a stale reference.  The callback fires once per completed
                # camera request in picamera2's own thread — no trigger lock involved.
                picam.post_callback = self._on_frame_callback
                self._picam     = picam
                self._connected = True
                try:
                    self._model = str(
                        picam.camera_properties.get("Model", "Pi Camera")
                    ).replace(",", " ") or "Pi Camera"
                except Exception:
                    self._model = "Pi Camera"
                # Pi Camera 3 (IMX708) has PDAF + full exposure/AWB controls
                self._has_af = "imx708" in self._model.lower()
                if self._has_af:
                    self._apply_controls_locked()
                print(f"[CameraPi] Connected: {self._model} @ {self.resolution}"
                      f" (AF controls: {self._has_af})")
                return True
            except Exception as exc:
                self._release_locked()
                msg = str(exc).replace(",", ";")
                print(f"[CameraPi] Connect failed: {exc}")
                self._send(f"CAM_ERR,{msg}\n")
                return False

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
        # Reconfigure live pipeline if connected
        with self._lock:
            if self._connected and self._picam is not None:
                try:
                    self._picam.stop()
                    self._configure_locked(self._picam)
                    self._picam.start()
                    # Reconfigure resets request controls — reapply the
                    # persisted Camera 3 settings so they survive a
                    # resolution change within the session.
                    if self._has_af and not self._apply_controls_locked():
                        return False
                except Exception as exc:
                    print(f"[CameraPi] Reconfigure failed: {exc}")
                    self._release_locked()
                    return False
        return True

    # ── Pi Camera 3 controls (no-ops on non-IMX708 hardware) ──────────────────

    def set_af_mode(self, mode: str) -> bool:
        """Set AF mode: 'continuous' | 'auto' | 'manual'. Persists."""
        mode = mode.strip().lower()
        if mode not in AF_MODES:
            return False
        if self._settings is not None:
            self._settings.set(KEY_AF_MODE, mode)
        return self._apply_controls()

    def af_trigger(self) -> None:
        """
        Run a one-shot AF cycle in a background thread and report the result
        via CAM_PI_AF_LOCKED / CAM_PI_AF_FAILED. No-op unless a Camera 3 is
        connected; overlapping triggers are ignored while one is in flight.
        """
        # Single-flight guard: claim the busy flag atomically. The flag lock is
        # never held while the cycle runs, so a second trigger returns
        # immediately instead of queueing behind the camera lock.
        with self._af_flag_lock:
            if self._af_busy:
                return
            if not (self._connected and self._has_af and self._picam is not None):
                self._send("CAM_PI_AF_FAILED\n")
                return
            self._af_busy = True

        def _run() -> None:
            ok = False
            try:
                with self._lock:
                    picam = self._picam
                    if picam is None:
                        return
                    # autofocus_cycle needs AfMode Auto; restore afterwards
                    picam.set_controls({"AfMode": AF_MODES["auto"]})
                    ok = bool(picam.autofocus_cycle())
                    self._apply_controls_locked()
            except Exception as exc:
                print(f"[CameraPi] AF cycle failed: {exc}")
                ok = False
            finally:
                with self._af_flag_lock:
                    self._af_busy = False
                self._send("CAM_PI_AF_LOCKED\n" if ok else "CAM_PI_AF_FAILED\n")

        threading.Thread(target=_run, name="picam-af", daemon=True).start()

    def set_lens_position(self, pos: float) -> bool:
        """Manual focus distance in dioptres (0 = infinity … 10 ≈ 10 cm)."""
        pos = min(max(pos, LENS_POS_MIN), LENS_POS_MAX)
        if self._settings is not None:
            self._settings.set(KEY_LENS_POS, round(pos, 2))
        return self._apply_controls()

    def set_exposure(self, value: str) -> bool:
        """
        'auto' or manual shutter time in whole milliseconds.
        Exposure and gain are jointly auto or jointly manual (the sensor's AE
        cannot pin only one), so 'auto' restores BOTH to automatic.
        """
        value = value.strip().lower()
        if value != "auto":
            try:
                value = str(min(max(int(float(value)), EXPOSURE_MS_MIN), EXPOSURE_MS_MAX))
            except (TypeError, ValueError):
                return False
        if self._settings is not None:
            self._settings.set(KEY_EXPOSURE, value)
            if value == "auto":
                self._settings.set(KEY_GAIN, "auto")
        return self._apply_controls()

    def set_gain(self, value: str) -> bool:
        """
        'auto' or manual analogue gain (≈ ISO/100).
        'auto' restores BOTH gain and exposure to automatic (see set_exposure).
        """
        value = value.strip().lower()
        if value != "auto":
            try:
                value = f"{min(max(float(value), GAIN_MIN), GAIN_MAX):g}"
            except (TypeError, ValueError):
                return False
        if self._settings is not None:
            self._settings.set(KEY_GAIN, value)
            if value == "auto":
                self._settings.set(KEY_EXPOSURE, "auto")
        return self._apply_controls()

    def set_awb(self, preset: str) -> bool:
        """AWB: 'auto' or a preset name from AWB_PRESETS."""
        preset = preset.strip()
        if preset.lower() == "auto":
            preset = "auto"
        if preset not in AWB_PRESETS:
            return False
        if self._settings is not None:
            self._settings.set(KEY_AWB, preset)
        return self._apply_controls()

    def _apply_controls(self) -> bool:
        with self._lock:
            return self._apply_controls_locked()

    def _apply_controls_locked(self) -> bool:
        """
        Push the persisted AF/exposure/gain/AWB settings to the live pipeline.
        Lock must be held. Returns True on success or when there is nothing to
        do (not connected / not a Camera 3 — settings still persist for later).
        """
        if not (self._connected and self._has_af and self._picam is not None):
            return True
        controls: dict = {}
        # Focus
        mode = self.af_mode
        controls["AfMode"] = AF_MODES[mode]
        if mode == "manual":
            controls["LensPosition"] = self.lens_position
        # Exposure / gain. Supported picamera2 semantics only:
        #   - both auto   → AeEnable True, ExposureTime/AnalogueGain omitted
        #   - any manual  → AeEnable False with concrete values for BOTH.
        # When one side is still "auto", freeze it at the camera's current
        # metered value (from capture metadata), persist that value, and
        # report it — so the clients always show the real applied state
        # instead of a fictitious "auto".
        exposure, gain = self.exposure, self.gain
        if exposure == "auto" and gain == "auto":
            controls["AeEnable"] = True
        else:
            exp_ms = int(exposure) if exposure != "auto" else None
            gain_v = float(gain) if gain != "auto" else None
            if exp_ms is None or gain_v is None:
                try:
                    meta = self._picam.capture_metadata()
                except Exception:
                    meta = {}
                if exp_ms is None:
                    exp_us = int(meta.get("ExposureTime") or 10000)
                    exp_ms = min(max(round(exp_us / 1000) or 1,
                                     EXPOSURE_MS_MIN), EXPOSURE_MS_MAX)
                    if self._settings is not None:
                        self._settings.set(KEY_EXPOSURE, str(exp_ms))
                if gain_v is None:
                    g = float(meta.get("AnalogueGain") or 1.0)
                    gain_v = min(max(g, GAIN_MIN), GAIN_MAX)
                    if self._settings is not None:
                        self._settings.set(KEY_GAIN, f"{gain_v:g}")
            controls["AeEnable"] = False
            controls["ExposureTime"] = exp_ms * 1000
            controls["AnalogueGain"] = gain_v
        # White balance
        awb = self.awb
        if awb == "auto":
            controls["AwbEnable"] = True
        else:
            controls["AwbEnable"] = False
            controls["AwbMode"] = AWB_PRESETS[awb]
        try:
            self._picam.set_controls(controls)
            return True
        except Exception as exc:
            print(f"[CameraPi] set_controls failed: {exc}")
            return False

    # ── Trigger ────────────────────────────────────────────────────────────────

    def trigger(self, shots: int = 1, af_pre_ms: int = 0) -> bool:
        """
        Capture `shots` JPEGs into the staging directory.
        af_pre_ms: optional settle delay before the first capture.
        Returns True if all captures completed without error.
        """
        if not self._connected or self._picam is None:
            return False
        ok = True
        for i in range(max(1, shots)):
            if i > 0:
                time.sleep(_BURST_GAP_S)
            if af_pre_ms > 0 and i == 0:
                time.sleep(af_pre_ms / 1000.0)
            tmp_path, final_path = _staging_paths("picam", i)
            try:
                with self._lock:
                    if self._picam is None:
                        return False
                    self._picam.capture_file(str(tmp_path))
                # Atomic publish: the uploader only sees fully-written JPEGs.
                os.replace(tmp_path, final_path)
            except Exception as exc:
                print(f"[CameraPi] Capture {i + 1} failed: {exc}")
                ok = False
        return ok

    # ── Push helpers ───────────────────────────────────────────────────────────

    def push_connected_info(self) -> None:
        """Emit CAM_PI,CONNECTED + model + resolution + Camera 3 controls."""
        self._send("CAM_PI,CONNECTED\n")
        self._send(f"CAM_PI_INFO,{self._model}\n")
        self._send(f"CAM_PI_RESOLUTION,{self.resolution}\n")
        self._send(f"CAM_PI_HAS_AF,{1 if self._has_af else 0}\n")
        if self._has_af:
            self._send(f"CAM_PI_AF_MODE,{self.af_mode}\n")
            self._send(f"CAM_PI_LENS_POS,{self.lens_position:g}\n")
            self._send(f"CAM_PI_EXPOSURE_CURRENT,{self.exposure}\n")
            self._send(f"CAM_PI_GAIN_CURRENT,{self.gain}\n")
            self._send(f"CAM_PI_AWB_CURRENT,{self.awb}\n")

    # ── Preview ────────────────────────────────────────────────────────────────

    def _on_frame_callback(self, request) -> None:
        """
        picamera2 post_callback — fires once per completed camera request in
        picamera2's own processing thread.  No trigger lock is held here, so
        a long manual exposure never blocks this path.

        Throttled to ~1 fps: encodes a lores JPEG in a daemon thread and
        stores it in _preview_cache so get_preview_jpeg() can return instantly.
        """
        if not self._lores_available:
            return
        now = time.monotonic()
        if now - self._last_preview_encode_t < 0.8:   # ~1-2 fps max
            return
        self._last_preview_encode_t = now

        # Extract lores image data from the completed request (fast — just a
        # memcopy; no new camera request is issued and no lock is needed).
        try:
            img = request.make_image("lores")
        except Exception:
            return

        # Encode JPEG off the callback thread so the camera pipeline is not held.
        def _encode(img_ref):
            import io
            try:
                buf = io.BytesIO()
                img_ref.save(buf, "JPEG", quality=70)
                with self._preview_cache_lock:
                    self._preview_cache = buf.getvalue()
            except Exception as exc:
                print(f"[CameraPi] Preview encode failed: {exc}")

        threading.Thread(target=_encode, args=(img,), daemon=True,
                         name="picam-preview-enc").start()

    def get_preview_jpeg(self) -> "bytes | None":
        """
        Return the latest cached preview JPEG without any camera interaction.
        Returns None when no frame has been cached yet (camera not connected
        or not yet warmed up).
        """
        with self._preview_cache_lock:
            return self._preview_cache

    # ── Internal ───────────────────────────────────────────────────────────────

    def _configure_locked(self, picam) -> None:
        """Apply the current still configuration. Lock must be held."""
        w, h = RESOLUTIONS[self.resolution]
        config = picam.create_still_configuration(
            main={"size": (w, h)},
            lores={"size": PREVIEW_SIZE},   # continuous low-res stream for /preview.jpg
        )
        picam.configure(config)
        self._lores_available = True

    def _release_locked(self) -> None:
        if self._picam is not None:
            try:
                # Remove callback before stopping so no new encode threads fire
                self._picam.post_callback = None
            except Exception:
                pass
            try:
                self._picam.stop()
            except Exception:
                pass
            try:
                self._picam.close()
            except Exception:
                pass
            self._picam = None
        self._connected       = False
        self._has_af          = False
        self._lores_available = False
        with self._preview_cache_lock:
            self._preview_cache = None


def _staging_paths(prefix: str, index: int) -> tuple[Path, Path]:
    """
    Return (tmp_path, final_path) for a capture. The camera writes to
    tmp_path (a hidden .tmp subdir on the same filesystem) and the caller
    os.replace()s it to final_path, so the Drive uploader never observes a
    partially-written JPEG.
    """
    tmp_dir = STAGING_DIR / ".tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    name = f"{prefix}_{stamp}_{index}.jpg"
    return tmp_dir / name, STAGING_DIR / name
