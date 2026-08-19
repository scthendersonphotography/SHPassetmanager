"""
USB Camera control via python-gphoto2 for Pi TX.

Wraps libgphoto2 to provide: detect, connect, trigger, settings read/write,
and background hot-plug detection. Completely optional — gracefully no-ops
when python-gphoto2 / libgphoto2 is not installed or no camera is connected.

gphoto2 config key names used (standardised across major camera brands):
  iso          — ISO speed  (e.g. "800", "Auto")
  aperture     — f-number   (e.g. "5.6")
  shutterspeed — shutter    (e.g. "1/250")  [shutterspeed2 on some bodies]

Supported cameras (non-exhaustive, all gphoto2-compatible):
  Canon EOS DSLR / R-series mirrorless (USB PTP)
  Nikon DSLR / Z-series mirrorless
  Sony Alpha  (USB PTP mode — enable in camera menu)
  Fujifilm X-series
  Olympus / OM System
  Pentax
"""

from __future__ import annotations

import threading
import time
from typing import Callable

# ── Optional import ────────────────────────────────────────────────────────────
GPHOTO2_AVAILABLE = False
try:
    import gphoto2 as gp          # python-gphoto2 >= 2.2
    GPHOTO2_AVAILABLE = True
except ImportError:
    pass

# gphoto2 config keys to probe, in priority order (vary by camera make/model)
_ISO_KEYS      = ("iso", "isoauto")
_APERTURE_KEYS = ("aperture", "f-number", "fnumber")
_SHUTTER_KEYS  = ("shutterspeed", "shutterspeed2", "exptime")

# Map short alias → tuple of raw gphoto2 key names
_KEY_MAP = {
    "iso":      _ISO_KEYS,
    "aperture": _APERTURE_KEYS,
    "ss":       _SHUTTER_KEYS,
}

_HOTPLUG_POLL_S   = 2.0    # how often the background thread checks for cameras
_CAPTURE_TIMEOUT  = 5000   # ms — gphoto2 wait_for_event timeout per shot


def _find_widget(config, keys: tuple):
    """Return first matching config widget, or None if not found."""
    for k in keys:
        try:
            return config.get_child_by_name(k)
        except Exception:
            pass
    return None


class CameraUSB:
    """
    Thread-safe USB camera driver.

    Lifecycle:
      start()      — launches background hot-plug thread
      stop()       — stops thread, releases camera handle
      connect()    — explicit connect (called by command_parser on CAMERA_DIRECT,6)
      disconnect() — explicit release

    Public thread-safe API (all silent no-ops when camera absent):
      trigger(shots, af_pre_ms) → bool
      get_info_str()            → "model,batt_pct,remaining"
      get_choices(alias)        → list[str]
      get_current_value(alias)  → str
      set_config_value(alias, val) → bool
      push_connected_info()     — emit CONNECTED + info + choices to outbox
    """

    def __init__(self, send_response: Callable[[str], None], settings=None) -> None:
        self._send      = send_response
        self._settings  = settings   # Settings instance for camMode guard in hot-plug
        self._lock      = threading.Lock()
        self._camera    = None   # gp.Camera instance when connected
        self._model     = ""
        self._connected = False
        self._running   = False
        self._thread: threading.Thread | None = None

    # ── Public properties ──────────────────────────────────────────────────────

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def model(self) -> str:
        return self._model

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start background hot-plug detection thread."""
        if not GPHOTO2_AVAILABLE:
            return
        self._running = True
        self._thread  = threading.Thread(
            target=self._hotplug_loop, daemon=True, name="cam-usb-hotplug"
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop background thread and release camera."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=6.0)
        self._release()

    # ── Connect / disconnect ───────────────────────────────────────────────────

    def connect(self) -> bool:
        """
        Attempt to init the first available USB camera.
        Returns True on success; False if not found or gphoto2 unavailable.
        Emits CAM_ERR,... on gphoto2 error; CAM_USB,NOT_FOUND when no cameras.
        """
        if not GPHOTO2_AVAILABLE:
            self._send("CAM_ERR,GPHOTO2_NOT_INSTALLED\n")
            return False

        with self._lock:
            if self._connected and self._camera is not None:
                return True   # already connected
            try:
                detected = gp.Camera.autodetect()
                if not detected:
                    return False   # caller emits NOT_FOUND if needed

                cam = gp.Camera()
                cam.init()
                self._camera    = cam
                self._connected = True
                self._model     = self._read_model_locked()
                print(f"[CameraUSB] Connected: {self._model}")
                return True

            except Exception as exc:
                self._camera    = None
                self._connected = False
                msg = str(exc).replace(",", ";")
                print(f"[CameraUSB] Connect failed: {exc}")
                self._send(f"CAM_ERR,{msg}\n")
                return False

    def disconnect(self) -> None:
        """Gracefully release the camera handle."""
        with self._lock:
            self._release_locked()

    # ── Query ──────────────────────────────────────────────────────────────────

    def get_info_str(self) -> str:
        """
        Return 'model,batt_pct,remaining_shots' for CAM_USB_INFO response.
        Falls back to empty strings when info is unavailable.
        """
        if not self._connected or self._camera is None:
            return ",,"
        try:
            with self._lock:
                summary = str(self._camera.get_summary())
            batt      = ""
            remaining = ""
            for line in summary.splitlines():
                lo = line.lower()
                if "battery" in lo:
                    for tok in line.split():
                        if tok.endswith("%"):
                            try:
                                batt = str(int(tok[:-1]))
                            except ValueError:
                                pass
                if any(k in lo for k in ("remaining", "shots available", "images")):
                    for tok in line.split():
                        try:
                            remaining = str(int(tok))
                            break
                        except ValueError:
                            pass
            model = self._model.replace(",", " ")
            return f"{model},{batt},{remaining}"
        except Exception:
            model = self._model.replace(",", " ")
            return f"{model},,"

    def get_choices(self, alias: str) -> list:
        """Return list of available choices for a setting alias, [] on error."""
        if not GPHOTO2_AVAILABLE or not self._connected or self._camera is None:
            return []
        keys = _KEY_MAP.get(alias, (alias,))
        try:
            with self._lock:
                config = self._camera.get_config()
            w = _find_widget(config, keys)
            if w is None:
                return []
            return [str(c) for c in w.get_choices()]
        except Exception:
            return []

    def get_current_value(self, alias: str) -> str:
        """Return current value of a setting alias, '' on error."""
        if not GPHOTO2_AVAILABLE or not self._connected or self._camera is None:
            return ""
        keys = _KEY_MAP.get(alias, (alias,))
        try:
            with self._lock:
                config = self._camera.get_config()
            w = _find_widget(config, keys)
            if w is None:
                return ""
            return str(w.get_value())
        except Exception:
            return ""

    # ── Settings write ─────────────────────────────────────────────────────────

    def set_config_value(self, alias: str, val: str) -> bool:
        """
        Set a named config value on the camera.
        alias: "iso" | "aperture" | "ss"  (or a raw gphoto2 config key)
        Returns True on success.
        """
        if not GPHOTO2_AVAILABLE or not self._connected or self._camera is None:
            return False
        keys = _KEY_MAP.get(alias, (alias,))
        try:
            with self._lock:
                config = self._camera.get_config()
                w      = _find_widget(config, keys)
                if w is None:
                    return False
                w.set_value(val)
                self._camera.set_config(config)
            return True
        except Exception as exc:
            print(f"[CameraUSB] set_config_value({alias!r},{val!r}) failed: {exc}")
            return False

    # ── Trigger ────────────────────────────────────────────────────────────────

    def trigger(self, shots: int = 1, af_pre_ms: int = 0) -> bool:
        """
        Fire the shutter `shots` times via gphoto2.

        af_pre_ms: wait this many ms before the first shot so the camera AF
                   has time to lock (gphoto2 capture itself also triggers AF
                   on most cameras, so this is primarily a configurable hold).

        Uses trigger_capture() (fire-and-forget) rather than capture() so the
        Pi daemon doesn't block waiting for image download.  The capture-complete
        event is drained with a bounded timeout to prevent the camera event queue
        from filling up.

        Returns True if all shots completed without error.
        """
        if not GPHOTO2_AVAILABLE or not self._connected or self._camera is None:
            return False

        ok = True
        for i in range(max(1, shots)):
            if i > 0:
                time.sleep(0.2)       # 200 ms inter-shot gap — matches GPIO burst gap
            if af_pre_ms > 0 and i == 0:
                time.sleep(af_pre_ms / 1000.0)
            try:
                with self._lock:
                    self._camera.trigger_capture()
                    # Drain capture event so camera queue doesn't fill
                    deadline = time.monotonic() + _CAPTURE_TIMEOUT / 1000.0
                    while time.monotonic() < deadline:
                        ev_type, _ = self._camera.wait_for_event(200)
                        if ev_type in (gp.GP_EVENT_FILE_ADDED,
                                       gp.GP_EVENT_CAPTURE_COMPLETE):
                            break
            except Exception as exc:
                print(f"[CameraUSB] Trigger shot {i+1} failed: {exc}")
                ok = False
        return ok

    # ── Push helpers ───────────────────────────────────────────────────────────

    def push_connected_info(self) -> None:
        """Emit CAM_USB,CONNECTED + info + all available choices/current values.

        Before reading current values from the camera, re-applies any previously
        saved ISO/aperture/SS settings stored in Settings so the camera's physical
        state matches the user's last-used values from a prior session.
        """
        self._send("CAM_USB,CONNECTED\n")
        self._send(f"CAM_USB_INFO,{self.get_info_str()}\n")

        # Re-apply stored settings so the camera (and the app) are in sync with
        # what the user last chose, not whatever the camera happened to power on to.
        # Settings are keyed by camera model (e.g. "usbIso_Canon EOS R5") so that
        # swapping camera bodies doesn't bleed one body's settings into another.
        if self._settings is not None:
            model = self._model
            for alias, base_key in (("iso", "usbIso"), ("aperture", "usbAperture"), ("ss", "usbSs")):
                model_key = f"{base_key}_{model}" if model else base_key
                stored = str(self._settings.get(model_key, "")).strip()
                if not stored and model:
                    # One-time migration: consume the old generic key (written by a
                    # previous firmware version that had no model awareness).  The key
                    # is cleared immediately after being promoted so that the NEXT body
                    # to connect does not inherit a value that belongs to this one.
                    generic = str(self._settings.get(base_key, "")).strip()
                    if generic:
                        print(f"[CameraUSB] Migrating {base_key}={generic!r} → {model_key} (consumed)")
                        self._settings.set(model_key, generic)
                        self._settings.set(base_key, "")   # consume — prevent cross-body bleed
                        stored = generic
                if stored:
                    ok = self.set_config_value(alias, stored)
                    if not ok:
                        print(f"[CameraUSB] Could not restore {alias}={stored!r} (camera may not support it)")

        for alias, prefix in (("iso", "CAM_ISO"), ("aperture", "CAM_APERTURE"), ("ss", "CAM_SS")):
            choices = self.get_choices(alias)
            if choices:
                self._send(f"{prefix}_CHOICES,{','.join(choices)}\n")
            current = self.get_current_value(alias)
            if current:
                self._send(f"{prefix}_CURRENT,{current}\n")

    # ── Hot-plug loop ──────────────────────────────────────────────────────────

    def _hotplug_loop(self) -> None:
        """
        Background thread: poll for camera presence every HOTPLUG_POLL_S seconds.
        Emits CAM_USB,CONNECTED / CAM_USB,DISCONNECTED into the outbox.
        Only auto-connects when a camera appears while USB mode (camMode=6) is
        already active; otherwise just updates presence state for the badge.
        """
        was_connected = False
        while self._running:
            try:
                detected = gp.Camera.autodetect()
                cam_present = len(detected) > 0
            except Exception:
                cam_present = False

            if cam_present and not was_connected and not self._connected:
                # Camera appeared — only auto-connect when USB camera mode is active.
                # Without this guard the hot-plug thread would claim the camera handle
                # while the user is in a non-USB mode and emit stale CAM_USB,CONNECTED
                # events that confuse the app's UI.
                cam_mode = (
                    int(self._settings.get("camMode", 0))
                    if self._settings is not None
                    else 0
                )
                if cam_mode != 6:
                    was_connected = self._connected
                    time.sleep(_HOTPLUG_POLL_S)
                    continue
                if self.connect():
                    self.push_connected_info()

            elif not cam_present and self._connected:
                # Camera disappeared
                with self._lock:
                    was_conn = self._connected
                    self._release_locked()
                if was_conn:
                    self._send("CAM_USB,DISCONNECTED\n")
                    print("[CameraUSB] Camera disconnected (hot-plug)")

            was_connected = self._connected
            time.sleep(_HOTPLUG_POLL_S)

    # ── Internal ───────────────────────────────────────────────────────────────

    def _read_model_locked(self) -> str:
        """Read camera model from summary.  Must be called with self._lock held."""
        if self._camera is None:
            return "USB Camera"
        try:
            summary = str(self._camera.get_summary())
            for line in summary.splitlines():
                if "model" in line.lower():
                    parts = line.split(":", 1)
                    if len(parts) == 2 and parts[1].strip():
                        return parts[1].strip()
        except Exception:
            pass
        return "USB Camera"

    def _release(self) -> None:
        with self._lock:
            self._release_locked()

    def _release_locked(self) -> None:
        """Release camera. Must be called with self._lock held."""
        if self._camera is not None:
            try:
                self._camera.exit()
            except Exception:
                pass
            self._camera = None
        self._connected = False
        self._model     = ""
