"""
Command parser for Pi TX — mirrors parseCommand() in laser_eye_v2_firmware.ino.

All commands and response formats are identical to the ESP32 firmware.
GET_INFO appends PLATFORM,PI as a second response line so the app can
detect Pi TX and show Pi-specific UI sections.

Commands handled in this task:
  GET_INFO, N,, S,, TRIG_MODE,, WAKE_MODE,, AF,, W,, FLUX,, LEDS,, LASER,
  SCHED,, TIME,, RATE,, T (manual trigger), T_WAKE

Stub stubs (implemented by later tasks):
  ESPNOW_*, GET_ESP_MAC        — ESP-NOW bridge (task #433)
  CAMERA_*, GET_CAM_INFO       — USB camera (task #434)
  GDRIVE_*                     — Google Drive upload (task #435)
  TIMELAPSE_*, ASTRO_*         — Time-lapse (task #436)
  RX_AF,, RX_WAKE_MODE,, RX_LEDS, — relayed via ESP-NOW bridge (task #433)
"""

from __future__ import annotations

import re
import secrets
import threading
import time
from typing import Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from settings import Settings
    from lidar import LiDARDriver
    from gpio_control import GPIOControl
    from trigger_engine import TriggerEngine

FIRMWARE_VERSION = "PI-1.0.0"
# Pi TX reports battery as 100 (mains powered).
# Override with a real reading if a Pi UPS / HAT is installed.
BATT_PCT = 100


def _parse_int(s: str, default: int = 0) -> int:
    try:
        return int(s.strip())
    except (ValueError, AttributeError):
        return default


def _clamp(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, v))


class CommandParser:
    def __init__(
        self,
        settings: "Settings",
        gpio: "GPIOControl",
        lidar: "LiDARDriver",
        trigger_engine: "TriggerEngine",
        send_response: Callable[[str], None],
    ) -> None:
        self._s = settings
        self._gpio = gpio
        self._lidar = lidar
        self._te = trigger_engine
        self._send = send_response
        self._bridge           = None  # set via set_bridge()
        self._camera           = None  # set via set_camera()
        self._camera_pi        = None  # set via set_camera_pi()   (mode 7)
        self._camera_webcam    = None  # set via set_camera_webcam() (mode 8)
        self._gdrive           = None  # set via set_gdrive()
        self._downloader       = None  # set via set_downloader()
        self._timelapse        = None  # set via set_timelapse()
        self._astro_scheduler  = None  # set via set_astro_scheduler()

    def set_bridge(self, bridge: "EspNowBridge") -> None:  # type: ignore[name-defined]
        """Wire the ESP-NOW bridge after both objects are created."""
        self._bridge = bridge

    def set_camera(self, camera: "CameraUSB") -> None:  # type: ignore[name-defined]
        """Wire the USB camera after both objects are created."""
        self._camera = camera

    def set_camera_pi(self, camera_pi) -> None:
        """Wire the CSI Pi Camera (camMode 7) after it is created."""
        self._camera_pi = camera_pi

    def set_camera_webcam(self, camera_webcam) -> None:
        """Wire the USB webcam (camMode 8) after it is created."""
        self._camera_webcam = camera_webcam

    def set_gdrive(self, gdrive) -> None:
        """Wire the Google Drive uploader after it is created."""
        self._gdrive = gdrive

    def set_downloader(self, downloader) -> None:
        """Wire the camera downloader after it is created."""
        self._downloader = downloader

    def set_timelapse(self, timelapse) -> None:
        """Wire the time-lapse engine after it is created."""
        self._timelapse = timelapse

    def set_astro_scheduler(self, scheduler) -> None:
        """Wire the astronomical scheduler after it is created."""
        self._astro_scheduler = scheduler

    # ── Entry point ───────────────────────────────────────────────────────────

    def handle(self, cmd: str) -> None:
        cmd = cmd.strip()
        if not cmd:
            return
        # Redact sensitive commands before logging so credentials never appear
        # in stdout / journald.  The original cmd is passed to _dispatch unchanged.
        if cmd.startswith("GDRIVE_SET_CREDS,"):
            print("[CMD] 'GDRIVE_SET_CREDS,[REDACTED]'")
        else:
            print(f"[CMD] {cmd!r}")
        self._dispatch(cmd)

    # ── Dispatcher ────────────────────────────────────────────────────────────

    def _dispatch(self, cmd: str) -> None:  # noqa: C901 (long but intentional)
        s = self._s

        # ── Manual trigger ────────────────────────────────────────────────────
        if cmd == "T":
            self._te.fire_manual()
            return

        if cmd == "T_WAKE":
            pulse_ms = s.get("pulseDur", 100)
            self._gpio.fire_wake(pulse_ms)
            self._send("WAKE_FIRED\n")
            return

        # ── GET_INFO ──────────────────────────────────────────────────────────
        if cmd == "GET_INFO":
            wake_sec = s.get("wakeIntervalMs", 300000) // 1000
            self._send(
                f"INFO,{s.get('name')},{FIRMWARE_VERSION},"
                f"{1 if s.get('wakeEn') else 0},{wake_sec},"
                f"{1 if s.get('statusLedEn') else 0},"
                f"{1 if s.get('powerLedEn') else 0},"
                f"{s.get('minFlux')},"
                f"{1 if s.get('espNowEn') else 0},"
                f"{s.get('trigMode')},"
                f"{s.get('wakeMode')},"
                f"{s.get('delayShot')},"
                f"{1 if s.get('afEnabled') else 0},"
                f"{s.get('afPreMs')},"
                f"{BATT_PCT},"
                f"{1 if s.get('schedEn') else 0},"
                f"{s.get('schedStart')},"
                f"{s.get('schedEnd')}\n"
            )
            # Pi-specific identifier — app uses this to show Pi-only UI sections
            self._send("PLATFORM,PI\n")
            return

        # ── Read-only settings query (web UI bootstrap) ──────────────────────
        # Emits the same S,... line as a settings write, without changing state.
        if cmd == "GET_SETTINGS":
            self._send(
                f"S,{s.get('minDist')},{s.get('maxDist')},"
                f"{s.get('cooldown')},{s.get('shots')},"
                f"{s.get('pulseDur')},{1 if s.get('invertTrig') else 0},"
                f"{s.get('delayShot')}\n"
            )
            return

        # ── Device name ───────────────────────────────────────────────────────
        if cmd.startswith("N,"):
            name = cmd[2:].strip()
            if name:
                s.set("name", name)
            return

        # ── Trigger mode ──────────────────────────────────────────────────────
        if cmd.startswith("TRIG_MODE,"):
            mode = _clamp(_parse_int(cmd[10:]), 0, 1)
            s.set("trigMode", mode)
            self._gpio.apply_trig_mode(mode)
            self._send(f"TRIG_MODE,{mode}\n")
            return

        # ── Wake mode ─────────────────────────────────────────────────────────
        if cmd.startswith("WAKE_MODE,"):
            mode = _clamp(_parse_int(cmd[10:]), 0, 1)
            s.set("wakeMode", mode)
            self._gpio.apply_wake_mode(mode)
            self._send(f"WAKE_MODE,{mode}\n")
            return

        # ── Settings: S,min,max,cooldown,shots,pulse,invert[,delayMs] ─────────
        if cmd.startswith("S,"):
            parts = cmd[2:].split(",")
            updates: dict = {}
            if len(parts) >= 1: updates["minDist"]   = _parse_int(parts[0])
            if len(parts) >= 2: updates["maxDist"]   = _parse_int(parts[1])
            if len(parts) >= 3: updates["cooldown"]  = max(0, _parse_int(parts[2]))
            if len(parts) >= 4: updates["shots"]     = _clamp(_parse_int(parts[3]), 1, 50)
            if len(parts) >= 5: updates["pulseDur"]  = _clamp(_parse_int(parts[4]), 100, 5000)
            if len(parts) >= 6: updates["invertTrig"]= (parts[5].strip() != "0")
            if len(parts) >= 7: updates["delayShot"] = _clamp(_parse_int(parts[6]), 0, 5000)
            s.update(updates)
            self._send(
                f"S,{s.get('minDist')},{s.get('maxDist')},"
                f"{s.get('cooldown')},{s.get('shots')},"
                f"{s.get('pulseDur')},{1 if s.get('invertTrig') else 0},"
                f"{s.get('delayShot')}\n"
            )
            return

        # ── Autofocus: AF,<en>,<preMs> ────────────────────────────────────────
        if cmd.startswith("AF,"):
            parts = cmd[3:].split(",")
            af_en = parts[0].strip() != "0"
            af_pre = _clamp(_parse_int(parts[1]) if len(parts) >= 2 else 500, 0, 5000)
            s.update({"afEnabled": af_en, "afPreMs": af_pre})
            self._send(f"AF,{1 if af_en else 0},{af_pre}\n")
            return

        # ── Wake: W,<en>,<intervalSec> ────────────────────────────────────────
        if cmd.startswith("W,"):
            rest = cmd[2:]
            comma = rest.find(",")
            w_en = rest[:comma].strip() != "0" if comma >= 0 else rest.strip() != "0"
            w_sec = _clamp(_parse_int(rest[comma + 1:]) if comma >= 0 else 300, 1, 86400)
            s.update({"wakeEn": w_en, "wakeIntervalMs": w_sec * 1000})
            self._te.reset_wake_timer()
            self._send(f"WAKE,{1 if w_en else 0},{w_sec}\n")
            return

        # ── Flux threshold: FLUX,<n> ──────────────────────────────────────────
        if cmd.startswith("FLUX,"):
            val = _clamp(_parse_int(cmd[5:]), 0, 65535)
            s.set("minFlux", val)
            self._send(f"FLUX,{val}\n")
            return

        # ── LEDs: LEDS,<statusEn>,<powerEn> ──────────────────────────────────
        if cmd.startswith("LEDS,") and not cmd.startswith("LEDS,RX"):
            parts = cmd[5:].split(",")
            if len(parts) >= 2:
                s_en = parts[0].strip() != "0"
                p_en = parts[1].strip() != "0"
                s.update({"statusLedEn": s_en, "powerLedEn": p_en})
                self._gpio.set_status_led(s_en)
                self._gpio.set_power_led(p_en)
                self._send(f"LEDS,{1 if s_en else 0},{1 if p_en else 0}\n")
            return

        # ── Laser: LASER,<0|1> ────────────────────────────────────────────────
        if cmd.startswith("LASER,"):
            on = cmd[6:].strip() != "0"
            self._gpio.set_laser(on)
            self._send(f"LASER,{1 if on else 0}\n")
            return

        # ── Trigger schedule: SCHED,<en>,<startMin>,<endMin>[,<unixSec>] ─────
        if cmd.startswith("SCHED,"):
            parts = cmd[6:].split(",")
            sched_en  = parts[0].strip() != "0" if len(parts) >= 1 else False
            start_min = _clamp(_parse_int(parts[1]) if len(parts) >= 2 else 0, 0, 1439)
            end_min   = _clamp(_parse_int(parts[2]) if len(parts) >= 3 else 1439, 0, 1439)
            # parts[3] = unixSec: Pi has real-time clock so no action needed
            s.update({"schedEn": sched_en, "schedStart": start_min, "schedEnd": end_min})
            self._send(f"SCHED,{1 if sched_en else 0},{start_min},{end_min}\n")
            return

        # ── Time sync: TIME,<unixSec> — no-op on Pi (system clock is NTP-synced)
        if cmd.startswith("TIME,"):
            return

        # ── Scan rate stub (hardware-fixed on Pi) ─────────────────────────────
        if cmd.startswith("RATE,"):
            return

        # ── ESP-NOW commands — routed through bridge co-processor ───────────────
        # When the bridge is connected, commands are forwarded to the ESP32
        # co-processor which speaks ESP-NOW.  The bridge responds asynchronously;
        # its response lines arrive via the reader thread and go straight to the
        # outbox, so the app receives them on the next GET /data or POST /cmd poll.
        #
        # When the bridge is absent, stub responses keep the app from hanging.

        if cmd.startswith("ESPNOW_MODE,"):
            en = cmd[12:].strip() != "0"
            s.set("espNowEn", en)
            # Always echo locally — app uses this ack regardless of bridge state
            self._send(f"ESPNOW_MODE,{1 if en else 0}\n")
            # Forward to bridge so it gates its fan-out sends
            if self._bridge and self._bridge.connected:
                self._bridge.send(cmd)
            return

        if cmd == "GET_ESP_MAC":
            if self._bridge and self._bridge.connected:
                # Bridge responds async: ESP_MAC,<mac>
                self._bridge.send("GET_ESP_MAC")
            else:
                self._send("ESP_MAC,00:00:00:00:00:00\n")
            return

        if cmd == "ESPNOW_GET_MAC":
            if self._bridge and self._bridge.connected:
                # Bridge responds async: ESPNOW_MAC,<list>  ESPNOW_RX_INFO,...
                self._bridge.send("ESPNOW_GET_MAC")
            else:
                self._send("ESPNOW_MAC,\n")
            return

        if cmd == "ESPNOW_REMOVE_ALL":
            if self._bridge and self._bridge.connected:
                # Bridge responds async: ESPNOW_REMOVE_ALL,OK
                self._bridge.send("ESPNOW_REMOVE_ALL")
            else:
                self._send("ESPNOW_REMOVE_ALL,OK\n")
            return

        if cmd.startswith("ESPNOW_PAIR,"):
            if self._bridge and self._bridge.connected:
                # Bridge responds async: ESPNOW_PAIR,OK / ERR_FULL / ERR_FORMAT
                self._bridge.send(cmd)
            else:
                self._send("ESPNOW_PAIR,ERR_NO_BRIDGE\n")
            return

        if cmd.startswith("ESPNOW_REMOVE,"):
            if self._bridge and self._bridge.connected:
                # Bridge responds async: ESPNOW_REMOVE,OK / ERR_NOT_FOUND
                self._bridge.send(cmd)
            else:
                self._send("ESPNOW_REMOVE,ERR_NOT_FOUND\n")
            return

        if cmd == "BRIDGE_OTA_MODE":
            # Switch the ESP-NOW bridge into WiFi OTA mode.  A fresh random
            # token is generated per request; the bridge requires it on every
            # /update upload, so only the app instance that requested OTA mode
            # (and received the token via its authenticated Pi connection) can
            # flash firmware.  The bridge acks asynchronously with
            # BRIDGE_OTA_MODE,OK,<token> before raising its AP; that ack
            # reaches the app via the outbox.  After OTA the bridge reboots
            # and re-announces ESPNOW_BRIDGE_READY, which re-syncs state.
            if self._bridge and self._bridge.connected:
                token = secrets.token_hex(16)
                self._bridge.send(f"BRIDGE_OTA_MODE,{token}")
            else:
                self._send("BRIDGE_OTA_MODE,ERR_NO_BRIDGE\n")
            return

        if cmd.startswith("ESPNOW_PAIR_CT,"):
            # Cable Trigger ESP-NOW peer — out of scope for this task
            self._send("ESPNOW_PAIR_CT,ERR_NO_BRIDGE\n")
            return

        # ── Laser Eye TX pairing — bridge stores the LX MAC and emits
        #    ESPNOW_LX_TRIGGER events when that MAC fires over ESP-NOW ────────
        if cmd.startswith("ESPNOW_PAIR_LXTX,"):
            if self._bridge and self._bridge.connected:
                # Bridge responds async: ESPNOW_PAIR_LXTX,OK / ERR_FORMAT
                self._bridge.send(cmd)
            else:
                self._send("ESPNOW_PAIR_LXTX,ERR_NO_BRIDGE\n")
            return

        if cmd == "ESPNOW_REMOVE_LXTX":
            if self._bridge and self._bridge.connected:
                # Bridge responds async: ESPNOW_REMOVE_LXTX,OK
                self._bridge.send(cmd)
            else:
                # Do not claim an unlink succeeded when the bridge could not
                # receive it. The app uses this error to keep recovery visible.
                self._send("ESPNOW_REMOVE_LXTX,ERR_NO_BRIDGE\n")
            return

        if cmd == "ESPNOW_GET_LXTX_MAC":
            if self._bridge and self._bridge.connected:
                # Bridge responds async: ESPNOW_LXTX_MAC,<mac>
                self._bridge.send(cmd)
            else:
                self._send("ESPNOW_LXTX_MAC,\n")
            return

        # ── RX relay commands — echo ack + forward to bridge via TX_ prefix ───
        # The TX_ prefix distinguishes bridge fan-out commands from Pi→bridge
        # management commands and matches the firmware's sendEspNow* pattern.

        if cmd.startswith("RX_AF,"):
            parts = cmd[6:].split(",")
            en_str  = parts[0].strip() if parts else "0"
            pre_str = parts[1].strip() if len(parts) >= 2 else "500"
            self._send(f"RX_AF,{en_str},{pre_str}\n")
            if self._bridge and self._bridge.connected:
                self._bridge.send(f"TX_RXAF,{en_str},{pre_str}")
            return

        if cmd.startswith("RX_WAKE_MODE,"):
            mode = _clamp(_parse_int(cmd[13:]), 0, 1)
            self._send(f"RX_WAKE_MODE,{mode}\n")
            if self._bridge and self._bridge.connected:
                self._bridge.send(f"TX_RXWAKEMODE,{mode}")
            return

        if cmd.startswith("RX_LEDS,"):
            parts = cmd[8:].split(",")
            s_en = parts[0].strip() if parts else "0"
            p_en = parts[1].strip() if len(parts) >= 2 else "0"
            self._send(f"RX_LEDS,{s_en},{p_en}\n")
            if self._bridge and self._bridge.connected:
                self._bridge.send(f"TX_RXLEDS,{s_en},{p_en}")
            return

        # ── USB Camera commands (mode 6, Pi TX only) ──────────────────────────

        if cmd.startswith("CAMERA_DIRECT,"):
            mode     = _clamp(_parse_int(cmd[14:]), 0, 8)
            prev     = int(s.get("camMode", 0))
            s.set("camMode", mode)
            self._send(f"CAMERA_DIRECT,{mode}\n")
            if mode == 0:
                self._send("CAM_WIFI,OFF\n")
            # Release camera handles whenever leaving their mode (any new mode,
            # not only 0). This prevents a handle from persisting while the
            # user is in another mode, and mirrors the hot-plug guards.
            if prev == 6 and mode != 6 and self._camera and self._camera.connected:
                self._camera.disconnect()
                self._send("CAM_USB,DISCONNECTED\n")
            if prev == 7 and mode != 7 and self._camera_pi and self._camera_pi.connected:
                self._camera_pi.disconnect()
                self._send("CAM_PI,DISCONNECTED\n")
            if prev == 8 and mode != 8 and self._camera_webcam and self._camera_webcam.connected:
                self._camera_webcam.disconnect()
                self._send("CAM_WEBCAM,DISCONNECTED\n")
            if mode == 6 and self._camera:
                if self._camera.connect():
                    self._camera.push_connected_info()
                else:
                    self._send("CAM_USB,NOT_FOUND\n")
            if mode == 7:
                if self._camera_pi and self._camera_pi.connect():
                    self._camera_pi.push_connected_info()
                else:
                    self._send("CAM_PI,NOT_FOUND\n")
            if mode == 8:
                if self._camera_webcam and self._camera_webcam.connect():
                    self._camera_webcam.push_connected_info()
                else:
                    self._send("CAM_WEBCAM,NOT_FOUND\n")
            return

        if cmd == "GET_CAM_INFO":
            mode = int(s.get("camMode", 0))
            if mode == 6:
                # Append stored ISO/aperture/SS so a poll after reconnect can
                # restore the app's dropdowns even when the camera is offline.
                # Use the model-specific key when the camera is connected so the
                # values shown match the body that is actually plugged in.
                model = self._camera.model if (self._camera and self._camera.connected) else ""
                iso = s.get(f"usbIso_{model}" if model else "usbIso", "")
                apt = s.get(f"usbAperture_{model}" if model else "usbAperture", "")
                ss  = s.get(f"usbSs_{model}" if model else "usbSs", "")
                self._send(f"CAM_INFO,6,0,,0,0,0,{iso},{apt},{ss}\n")
                if self._camera and self._camera.connected:
                    self._send(f"CAM_USB_INFO,{self._camera.get_info_str()}\n")
            elif mode == 7:
                self._send("CAM_INFO,7,0,,0,0,0\n")
                if self._camera_pi and self._camera_pi.connected:
                    self._camera_pi.push_connected_info()
                else:
                    self._send("CAM_PI,NOT_FOUND\n")
            elif mode == 8:
                self._send("CAM_INFO,8,0,,0,0,0\n")
                if self._camera_webcam and self._camera_webcam.connected:
                    self._camera_webcam.push_connected_info()
                else:
                    self._send("CAM_WEBCAM,NOT_FOUND\n")
            else:
                self._send(f"CAM_INFO,{mode},0,,0,0,0\n")
            return

        if cmd == "CAMERA_TEST_SHUTTER":
            mode = int(s.get("camMode", 0))
            if mode == 6 and self._camera and self._camera.connected:
                ok = self._camera.trigger(shots=1, af_pre_ms=0)
                self._send("CAM_SHUTTER,OK\n" if ok else "CAM_SHUTTER,FAIL\n")
            elif mode == 7 and self._camera_pi and self._camera_pi.connected:
                ok = self._camera_pi.trigger(shots=1, af_pre_ms=0)
                self._send("CAM_SHUTTER,OK\n" if ok else "CAM_SHUTTER,FAIL\n")
            elif mode == 8 and self._camera_webcam and self._camera_webcam.connected:
                ok = self._camera_webcam.trigger(shots=1, af_pre_ms=0)
                self._send("CAM_SHUTTER,OK\n" if ok else "CAM_SHUTTER,FAIL\n")
            else:
                self._send("CAM_SHUTTER,NO_MODE\n")
            return

        # CAM_RESOLUTION,<alias> — set resolution for the active direct-capture
        # camera (mode 7: 1080p|4K, mode 8: 720p|1080p). Ack is mode-specific
        # so both control panels can update the right picker unambiguously.
        if cmd.startswith("CAM_RESOLUTION,"):
            val  = cmd[15:].strip()
            mode = int(s.get("camMode", 0))
            if mode == 7 and self._camera_pi and self._camera_pi.set_resolution(val):
                self._send(f"CAM_PI_RESOLUTION,{self._camera_pi.resolution}\n")
            elif mode == 8 and self._camera_webcam and self._camera_webcam.set_resolution(val):
                self._send(f"CAM_WEBCAM_RESOLUTION,{self._camera_webcam.resolution}\n")
            else:
                self._send("CAM_ERR,RESOLUTION_FAIL\n")
            return

        # ── Pi Camera 3 controls (mode 7, IMX708 only) ────────────────────────

        if cmd.startswith("CAM_PI_AF_MODE,"):
            val = cmd[15:].strip().lower()
            if val in ("continuous", "auto", "manual") and self._camera_pi and self._camera_pi.set_af_mode(val):
                self._send(f"CAM_PI_AF_MODE,{self._camera_pi.af_mode}\n")
            else:
                self._send("CAM_ERR,AF_MODE_FAIL\n")
            return

        if cmd == "CAM_PI_AF_TRIGGER":
            if self._camera_pi:
                self._camera_pi.af_trigger()   # async; replies LOCKED/FAILED
            else:
                self._send("CAM_PI_AF_FAILED\n")
            return

        if cmd.startswith("CAM_PI_LENS_POS,"):
            try:
                pos = float(cmd[16:].strip())
            except ValueError:
                self._send("CAM_ERR,LENS_POS_FAIL\n")
                return
            if self._camera_pi and self._camera_pi.set_lens_position(pos):
                self._send(f"CAM_PI_LENS_POS,{self._camera_pi.lens_position:g}\n")
            else:
                self._send("CAM_ERR,LENS_POS_FAIL\n")
            return

        if cmd.startswith("CAM_PI_EXPOSURE,"):
            val = cmd[16:].strip()
            if self._camera_pi and self._camera_pi.set_exposure(val):
                # Manual exposure may freeze a still-auto gain at its metered
                # value (and vice versa) — report both so clients show the
                # real applied state.
                self._send(f"CAM_PI_EXPOSURE_CURRENT,{self._camera_pi.exposure}\n")
                self._send(f"CAM_PI_GAIN_CURRENT,{self._camera_pi.gain}\n")
            else:
                self._send("CAM_ERR,EXPOSURE_FAIL\n")
            return

        if cmd.startswith("CAM_PI_GAIN,"):
            val = cmd[12:].strip()
            if self._camera_pi and self._camera_pi.set_gain(val):
                self._send(f"CAM_PI_GAIN_CURRENT,{self._camera_pi.gain}\n")
                self._send(f"CAM_PI_EXPOSURE_CURRENT,{self._camera_pi.exposure}\n")
            else:
                self._send("CAM_ERR,GAIN_FAIL\n")
            return

        if cmd.startswith("CAM_PI_AWB,"):
            val = cmd[11:].strip()
            if self._camera_pi and self._camera_pi.set_awb(val):
                self._send(f"CAM_PI_AWB_CURRENT,{self._camera_pi.awb}\n")
            else:
                self._send("CAM_ERR,AWB_FAIL\n")
            return

        if cmd.startswith("CAM_ISO,"):
            val = cmd[8:].strip()
            if self._camera and self._camera.set_config_value("iso", val):
                model = self._camera.model
                s.set(f"usbIso_{model}" if model else "usbIso", val)
                self._send(f"CAM_ISO_CURRENT,{val}\n")
            else:
                self._send("CAM_ERR,ISO_FAIL\n")
            return

        if cmd.startswith("CAM_APERTURE,"):
            val = cmd[13:].strip()
            if self._camera and self._camera.set_config_value("aperture", val):
                model = self._camera.model
                s.set(f"usbAperture_{model}" if model else "usbAperture", val)
                self._send(f"CAM_APERTURE_CURRENT,{val}\n")
            else:
                self._send("CAM_ERR,APERTURE_FAIL\n")
            return

        if cmd.startswith("CAM_SS,"):
            # CAM_SS = shutter speed (not CAM_SHUTTER,OK which is a response)
            val = cmd[7:].strip()
            if self._camera and self._camera.set_config_value("ss", val):
                model = self._camera.model
                s.set(f"usbSs_{model}" if model else "usbSs", val)
                self._send(f"CAM_SS_CURRENT,{val}\n")
            else:
                self._send("CAM_ERR,SS_FAIL\n")
            return

        if cmd.startswith("CAM_BURST,"):
            n = _clamp(_parse_int(cmd[10:]), 1, 50)
            s.set("usbBurst", n)
            self._send(f"CAM_BURST,{n}\n")
            return

        if cmd.startswith("CAM_AF,"):
            en = _parse_int(cmd[7:]) != 0
            s.set("usbAfEn", en)
            self._send(f"CAM_AF,{1 if en else 0}\n")
            return

        if cmd.startswith("CAMERA_") or cmd.startswith("CAM_"):
            self._send("CAM_ERR,UNKNOWN_CMD\n")
            return

        # ── Google Drive commands ─────────────────────────────────────────────
        if cmd == "GDRIVE_AUTH_URL":
            if self._gdrive:
                self._gdrive.get_auth_url()
            else:
                self._send("GDRIVE_ERR,GDRIVE_NOT_AVAILABLE\n")
            return

        if cmd == "GDRIVE_AUTH_CODE" or cmd.startswith("GDRIVE_AUTH_CODE,"):
            # In the device flow the Pi polls automatically; this triggers an
            # immediate status reply so the app can confirm auth completed.
            if self._gdrive:
                self._gdrive.check_auth()
            else:
                self._send("GDRIVE_ERR,GDRIVE_NOT_AVAILABLE\n")
            return

        if cmd == "GDRIVE_AUTH_CANCEL":
            if self._gdrive:
                self._gdrive.cancel_auth()
            else:
                self._send("GDRIVE_AUTH_CANCELLED\n")
            return

        if cmd == "GDRIVE_STATUS":
            if self._gdrive:
                self._gdrive.get_status()
            else:
                self._send("GDRIVE_STATUS,unavailable,,0\n")
            return

        if cmd.startswith("GDRIVE_FOLDER,"):
            folder_name = cmd[14:].strip()
            if self._gdrive:
                self._gdrive.set_folder(folder_name)
            return

        if cmd.startswith("GDRIVE_UPLOAD_LAST,"):
            n = _clamp(_parse_int(cmd[19:]), 1, 500)
            if self._gdrive and self._downloader:
                threading.Thread(
                    target=self._run_gdrive_upload,
                    args=(n, False),
                    daemon=True,
                    name="gdrive-upload-last",
                ).start()
            else:
                self._send("GDRIVE_ERR,NOT_AVAILABLE\n")
            return

        if cmd == "GDRIVE_UPLOAD_ALL":
            if self._gdrive and self._downloader:
                threading.Thread(
                    target=self._run_gdrive_upload,
                    args=(0, True),
                    daemon=True,
                    name="gdrive-upload-all",
                ).start()
            else:
                self._send("GDRIVE_ERR,NOT_AVAILABLE\n")
            return

        # GDRIVE_SET_CREDS,<client_id>,<client_secret>
        # Writes credentials to ~/.pi-tx/gdrive_client_secrets.json so the user
        # can set up Drive without SSH-ing into the Pi.  Responds GDRIVE_CREDS_OK
        # or GDRIVE_ERR,WRITE_FAILED.
        if cmd.startswith("GDRIVE_SET_CREDS,"):
            rest = cmd[17:].strip()
            comma = rest.find(",")
            if comma < 0:
                self._send("GDRIVE_ERR,WRITE_FAILED\n")
                return
            client_id     = rest[:comma].strip()
            client_secret = rest[comma + 1:].strip()
            if self._gdrive:
                self._gdrive.set_credentials(client_id, client_secret)
            else:
                self._send("GDRIVE_ERR,GDRIVE_NOT_AVAILABLE\n")
            return

        if cmd.startswith("GDRIVE_"):
            self._send("GDRIVE_ERR,UNKNOWN_CMD\n")
            return

        # ── Time-lapse commands (task #436) ───────────────────────────────────
        # TIMELAPSE_START,<intervalSec>,<durationSec>
        if cmd.startswith("TIMELAPSE_START,"):
            parts = cmd[16:].split(",")
            interval_sec = max(1, _parse_int(parts[0]))
            duration_sec = max(1, _parse_int(parts[1]) if len(parts) >= 2 else 3600)
            if self._timelapse:
                started = self._timelapse.start(interval_sec, duration_sec)
                if started:
                    self._send(self._timelapse.status_line())
                else:
                    self._send("TIMELAPSE_ERR,BUSY\n")
            else:
                self._send("TIMELAPSE_ERR,NOT_AVAILABLE\n")
            return

        if cmd == "TIMELAPSE_STOP":
            if self._timelapse and self._timelapse.running:
                self._timelapse.stop()
            self._send("TIMELAPSE_STOPPING\n")
            return

        if cmd == "TIMELAPSE_STATUS":
            if self._timelapse:
                self._send(self._timelapse.status_line())
            else:
                self._send("TIMELAPSE,idle,0,0\n")
            return

        if cmd.startswith("TIMELAPSE_"):
            self._send("TIMELAPSE_ERR,UNKNOWN_CMD\n")
            return

        # ── Astronomical commands (task #436) ─────────────────────────────────
        # ASTRO_LOCATION,<lat>,<lon>
        if cmd.startswith("ASTRO_LOCATION,"):
            parts = cmd[15:].split(",")
            if len(parts) < 2:
                self._send("ASTRO_ERR,BAD_LOCATION\n")
                return
            try:
                lat = float(parts[0])
                lon = float(parts[1])
            except ValueError:
                self._send("ASTRO_ERR,BAD_LOCATION\n")
                return
            if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
                self._send("ASTRO_ERR,BAD_LOCATION\n")
                return
            s.update({"astroLat": lat, "astroLon": lon})
            if self._astro_scheduler:
                self._astro_scheduler.set_location(lat, lon)
            self._send(f"ASTRO_LOCATION,{lat},{lon}\n")
            return

        # ASTRO_PREVIEW — emit today's + tomorrow's event times
        if cmd == "ASTRO_PREVIEW":
            if self._astro_scheduler:
                lines = self._astro_scheduler.get_preview_lines(days=2)
                self._send("ASTRO_PREVIEW_BEGIN\n")
                for line in lines:
                    self._send(line)
            else:
                self._send("ASTRO_ERR,NOT_AVAILABLE\n")
            return

        # ASTRO_EVENT,<event>,<offsetMin>,<durationSec>,<intervalSec>
        if cmd.startswith("ASTRO_EVENT,"):
            parts = cmd[12:].split(",")
            if len(parts) < 4:
                self._send("ASTRO_ERR,BAD_PARAMS\n")
                return
            event        = parts[0].strip()
            offset_min   = _parse_int(parts[1])
            duration_sec = max(1, _parse_int(parts[2]))
            interval_sec = max(1, _parse_int(parts[3]))
            if self._astro_scheduler:
                try:
                    sched_id = self._astro_scheduler.add_schedule(
                        event, offset_min, duration_sec, interval_sec
                    )
                    self._send(
                        f"ASTRO_SCHED,{sched_id},{event},{offset_min},"
                        f"{duration_sec},{interval_sec}\n"
                    )
                except ValueError as exc:
                    self._send(f"ASTRO_ERR,{exc}\n")
            else:
                self._send("ASTRO_ERR,NOT_AVAILABLE\n")
            return

        # ASTRO_SCHEDULE_LIST — emit all active entries
        if cmd == "ASTRO_SCHEDULE_LIST":
            if self._astro_scheduler:
                self._send("ASTRO_SCHED_LIST_BEGIN\n")
                for entry in self._astro_scheduler.get_schedules():
                    self._send(
                        f"ASTRO_SCHED,{entry['id']},{entry['event']},"
                        f"{entry['offsetMin']},{entry['durationSec']},"
                        f"{entry['intervalSec']}\n"
                    )
                self._send("ASTRO_SCHED_LIST_END\n")
            else:
                self._send("ASTRO_SCHED_LIST_BEGIN\n")
                self._send("ASTRO_SCHED_LIST_END\n")
            return

        # ASTRO_SCHEDULE_DELETE,<id> — remove one entry by id
        if cmd.startswith("ASTRO_SCHEDULE_DELETE,"):
            sched_id = cmd[22:].strip()
            if self._astro_scheduler:
                found = self._astro_scheduler.remove_schedule(sched_id)
                if found:
                    self._send(f"ASTRO_SCHED_DELETED,{sched_id}\n")
                else:
                    self._send(f"ASTRO_ERR,NOT_FOUND,{sched_id}\n")
            else:
                self._send("ASTRO_ERR,NOT_AVAILABLE\n")
            return

        # ASTRO_SCHEDULE_CLEAR — remove all entries
        if cmd == "ASTRO_SCHEDULE_CLEAR":
            if self._astro_scheduler:
                self._astro_scheduler.clear_schedules()
            self._send("ASTRO_SCHED_CLEARED\n")
            return

        if cmd.startswith("ASTRO_"):
            self._send("ASTRO_ERR,UNKNOWN_CMD\n")
            return

        print(f"[CMD] Unknown command: {cmd!r}")

    # ── Google Drive upload helper ─────────────────────────────────────────────

    def _run_gdrive_upload(self, n: int, upload_all: bool) -> None:
        """
        Background thread: download images from the USB camera, then upload to Drive.
        `n` = how many images for download_last_n; ignored when upload_all=True.
        Emits GDRIVE_PROGRESS,<done>,<total*2> (first half = download, second = upload)
        then GDRIVE_DONE,<count> or GDRIVE_ERR,<reason>.
        """
        from datetime import datetime

        # Direct-capture modes (7 = Pi Camera, 8 = webcam) stage JPEGs locally
        # at capture time — there is nothing to download, so upload straight
        # from the staging directory. Routing is driven by capture provenance,
        # not just the currently selected source: even after the user switches
        # modes or disconnects the camera, previously staged direct captures
        # remain uploadable (as long as no DSLR flow takes precedence).
        cam_mode = int(self._s.get("camMode", 0))
        dslr_connected = bool(self._camera and self._camera.connected)
        if cam_mode in (7, 8) or (not dslr_connected and self._staged_direct_files()):
            self._run_gdrive_upload_staged(n, upload_all)
            return

        if not dslr_connected:
            self._send("GDRIVE_ERR,NO_CAMERA\n")
            return

        # Phase 1: Download from camera ──────────────────────────────────────
        dl_total    = [0]
        cam_files   = []

        def dl_progress(done: int, total: int) -> None:
            dl_total[0] = total
            combined    = max(total, 1) * 2
            self._send(f"GDRIVE_PROGRESS,{done},{combined}\n")

        target_dl_count = 0
        if upload_all:
            local_paths, cam_files_result, target_dl_count = self._downloader.download_all_new(
                self._camera, dl_progress
            )
            cam_files = cam_files_result
        else:
            local_paths, target_dl_count = self._downloader.download_last_n(
                self._camera, n, dl_progress
            )

        if not local_paths:
            if target_dl_count > 0:
                # Files existed on the camera but every download failed — signal error
                # so the user knows something went wrong rather than seeing a silent 0.
                self._send("GDRIVE_ERR,DOWNLOAD_FAILED\n")
            else:
                # Camera is empty or no new files since last cursor — not an error.
                self._send("GDRIVE_DONE,0\n")
            return

        # Phase 2: Upload to Google Drive ────────────────────────────────────
        total_per_phase = dl_total[0] or len(local_paths)
        date_str        = datetime.now().strftime("%Y-%m-%d")

        def up_progress(done: int, _total: int) -> None:
            combined = total_per_phase * 2
            self._send(f"GDRIVE_PROGRESS,{total_per_phase + done},{combined}\n")

        result = self._gdrive.upload_files(local_paths, date_str, up_progress)
        if result is None:
            # upload_files already emitted GDRIVE_ERR,BUSY — stop here.
            return

        successful, upload_total = result

        # Advance idempotency cursor ONLY when every download AND upload succeeded.
        # If anything failed, leave cursor in place so the next UPLOAD_ALL retries them.
        # upload_files() pre-fetches Drive names and counts already-uploaded files as
        # successful, so partial retries are idempotent without re-uploading them.
        all_downloads_ok = (len(local_paths) == target_dl_count)
        all_uploads_ok   = (successful == upload_total)
        all_ok           = all_downloads_ok and all_uploads_ok

        if upload_all and cam_files:
            if all_ok:
                self._downloader.advance_cursor(cam_files)
            else:
                failed = (target_dl_count - len(local_paths)) + (upload_total - successful)
                self._send(f"GDRIVE_ERR,PARTIAL_RETRY,{failed}\n")
        elif not all_ok:
            # Last-N partial failure — surface the same terminal error so users know
            # images were skipped rather than receiving a silent partial GDRIVE_DONE.
            failed = (target_dl_count - len(local_paths)) + (upload_total - successful)
            self._send(f"GDRIVE_ERR,PARTIAL_RETRY,{failed}\n")

        self._send(f"GDRIVE_DONE,{successful}\n")

    @staticmethod
    def _staged_direct_files() -> list:
        """
        Return completed direct-capture JPEGs (Pi Camera / webcam) in the
        staging dir, sorted by mtime. Only picks the picam_/webcam_ prefixes so
        DSLR downloads staged by camera_download are never double-routed here.
        Captures are published atomically (tmp-dir + os.replace), so any file
        visible under these names is complete — no partial-write race.
        """
        from camera_download import STAGING_DIR

        try:
            files = [
                p
                for pattern in ("picam_*.jpg", "webcam_*.jpg")
                for p in STAGING_DIR.glob(pattern)
                if p.is_file()
            ]
            return sorted(files, key=lambda p: p.stat().st_mtime)
        except OSError:
            return []

    def _run_gdrive_upload_staged(self, n: int, upload_all: bool) -> None:
        """
        Upload already-staged images (Pi Camera / webcam captures) from the
        shared staging directory to Google Drive. No download phase.
        """
        from datetime import datetime

        files = self._staged_direct_files()

        if not upload_all:
            files = files[-max(1, n):]

        if not files:
            self._send("GDRIVE_DONE,0\n")
            return

        total    = len(files)
        date_str = datetime.now().strftime("%Y-%m-%d")

        def up_progress(done: int, _total: int) -> None:
            self._send(f"GDRIVE_PROGRESS,{done},{total}\n")

        result = self._gdrive.upload_files([str(p) for p in files], date_str, up_progress)
        if result is None:
            return   # upload_files already emitted GDRIVE_ERR,BUSY

        successful, upload_total = result
        if successful != upload_total:
            self._send(f"GDRIVE_ERR,PARTIAL_RETRY,{upload_total - successful}\n")
        self._send(f"GDRIVE_DONE,{successful}\n")
