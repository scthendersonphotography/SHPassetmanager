"""
LiDAR trigger engine and wake pulse timer for Pi TX.

Trigger logic mirrors the ESP32 runTriggerLogic() exactly:

  Zone detection (same as ESP32):
    inZone = dist > 0 AND dist in [minDist, maxDist] AND flux >= minFlux
    objectInZone flag: set on zone entry, cleared on zone exit.

  Firing semantics (same as ESP32 — NOT edge-only):
    While in zone AND cooldown elapsed AND no burst in progress → fire.
    After burst: lastTriggerTime stamped from END of burst (so cooldown
    is measured from the last completed shot, matching ESP32 line 1330).
    Object can re-trigger on every cooldown tick while it stays in zone.

  AF pre-pulse (matches triggerCamera() in ESP32 firmware, lines 1184-1228):
    1. Assert WAKE pin to active level (hold — not released yet)
    2. Wait afPreMs
    3. Fire each TRIGGER pin pulse (200 ms gap between burst shots)
    4. Release WAKE pin after ALL shots complete
    The wake pin is held for the entire burst — same as ESP32.

  Schedule gate: uses datetime.utcnow() — Pi has NTP clock, no millis() trick.

Wake pulse timer:
  Background thread fires wake pin every wakeIntervalMs when enabled.
  Not gated by schedule window (matches ESP32 runWakeTrigger behaviour).
"""

import threading
import time
from datetime import datetime, timezone
from typing import Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from settings import Settings
    from lidar import LiDARDriver
    from gpio_control import GPIOControl

# Inter-shot gap (ms) for burst sequences — matches ESP32 firmware line 1221
BURST_INTER_SHOT_MS = 200


class TriggerEngine:
    def __init__(
        self,
        settings: "Settings",
        gpio: "GPIOControl",
        lidar: "LiDARDriver",
        send_response: Callable[[str], None],
    ) -> None:
        self._settings = settings
        self._gpio = gpio
        self._lidar = lidar
        self._send_response = send_response
        self._bridge = None   # set via set_bridge() after bridge is created
        self._camera = None          # set via set_camera() after CameraUSB is created
        self._camera_pi = None       # set via set_camera_pi() — CSI Pi Camera (mode 7)
        self._camera_webcam = None   # set via set_camera_webcam() — USB webcam (mode 8)

        self._running = False
        self._trig_thread: threading.Thread | None = None
        self._wake_thread: threading.Thread | None = None

        # State variables mirror ESP32 globals
        self._object_in_zone = False
        self._last_trigger_monotonic = 0.0   # updated after burst completes
        self._last_wake_monotonic = 0.0
        self._in_burst = False               # prevents concurrent trigger sequences

    def set_bridge(self, bridge: "EspNowBridge") -> None:  # type: ignore[name-defined]
        """Wire the ESP-NOW bridge so trigger/wake pulses are fan-out to RX modules."""
        self._bridge = bridge

    def set_camera(self, camera: "CameraUSB") -> None:  # type: ignore[name-defined]
        """Wire the USB camera so trigger sequences use gphoto2 instead of GPIO."""
        self._camera = camera

    def set_camera_pi(self, camera_pi: "CameraPi") -> None:  # type: ignore[name-defined]
        """Wire the CSI Pi Camera (camMode 7) so triggers capture via picamera2."""
        self._camera_pi = camera_pi

    def set_camera_webcam(self, camera_webcam: "CameraWebcam") -> None:  # type: ignore[name-defined]
        """Wire the USB webcam (camMode 8) so triggers capture via OpenCV."""
        self._camera_webcam = camera_webcam

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        self._running = True
        self._last_wake_monotonic = time.monotonic()
        self._trig_thread = threading.Thread(
            target=self._trigger_loop, daemon=True, name="trigger-engine"
        )
        self._wake_thread = threading.Thread(
            target=self._wake_loop, daemon=True, name="wake-timer"
        )
        self._trig_thread.start()
        self._wake_thread.start()
        print("[Trigger] Engine started")

    def stop(self) -> None:
        self._running = False
        for t in (self._trig_thread, self._wake_thread):
            if t:
                t.join(timeout=2.0)
        print("[Trigger] Engine stopped")

    def reset_wake_timer(self) -> None:
        """Call when wakeIntervalMs changes so the countdown restarts cleanly."""
        self._last_wake_monotonic = time.monotonic()

    def fire_manual(self) -> bool:
        """
        Immediately fire one trigger sequence (T command / manual button).

        Returns True when a burst was actually started, False when the engine
        is already mid-burst (caller should not count this as a shot taken).
        """
        if self._in_burst:
            return False
        self._in_burst = True
        threading.Thread(target=self._do_trigger_sequence, daemon=True).start()
        return True

    def fire_espnow_lx(self, pulse_ms: int) -> bool:
        """
        Fire one camera shot on behalf of the paired Laser Eye TX (ESP-NOW).

        Same path as fire_manual(); pulse_ms is the pulse width requested by
        the Laser Eye TX (informational for the USB camera path — the shot is
        a gphoto2 capture, not a timed GPIO pulse).
        """
        if self._in_burst:
            print(f"[Trigger] [ESP-NOW-LX] trigger ignored — burst in progress (pulse={pulse_ms} ms)")
            return False
        print(f"[Trigger] [ESP-NOW-LX] trigger from Laser Eye TX (pulse={pulse_ms} ms)")
        self._in_burst = True
        threading.Thread(target=self._do_trigger_sequence, daemon=True).start()
        return True

    # ── Schedule window ───────────────────────────────────────────────────────

    def _inside_window(self) -> bool:
        """Return True when the current UTC time falls inside the active schedule.
        Always True when schedule is disabled (matches ESP32 isInsideScheduleWindow).
        """
        if not self._settings.get("schedEn", False):
            return True
        now = datetime.now(timezone.utc)
        day_min = now.hour * 60 + now.minute
        start: int = self._settings.get("schedStart", 0)
        end: int   = self._settings.get("schedEnd",   1439)
        if start <= end:
            return start <= day_min <= end
        else:
            # Midnight-spanning window e.g. 22:00 → 06:00
            return day_min >= start or day_min <= end

    # ── Trigger loop (~100 Hz) ────────────────────────────────────────────────

    def _trigger_loop(self) -> None:
        """
        Mirrors ESP32 runTriggerLogic() called from the main loop at ~100 Hz.

        Fires whenever: in zone AND cooldown elapsed AND schedule open AND no
        burst already in progress. Does NOT gate on zone-entry edge — object
        can re-trigger on every completed cooldown while it stays in zone.
        """
        while self._running:
            dist  = self._lidar.distance
            flux  = self._lidar.flux

            min_d:       int = self._settings.get("minDist",   0)
            max_d:       int = self._settings.get("maxDist",   800)
            min_flux:    int = self._settings.get("minFlux",   20)
            cooldown_ms: int = self._settings.get("cooldown",  1000)

            # dist > 0 guards against sensor-absent 0-reads (same as ESP32 tfDist > 0)
            in_zone = (dist > 0 and min_d <= dist <= max_d and flux >= min_flux)

            if in_zone:
                if not self._object_in_zone:
                    self._object_in_zone = True

                if not self._in_burst:
                    now = time.monotonic()
                    elapsed_ms = (now - self._last_trigger_monotonic) * 1000.0
                    if elapsed_ms >= cooldown_ms and self._inside_window():
                        # Guard flag set before spawning so the loop doesn't re-enter
                        self._in_burst = True
                        threading.Thread(
                            target=self._do_trigger_sequence, daemon=True
                        ).start()

            elif self._object_in_zone:
                self._object_in_zone = False

            time.sleep(0.01)

    # ── Wake loop ─────────────────────────────────────────────────────────────

    def _wake_loop(self) -> None:
        while self._running:
            if self._settings.get("wakeEn", False):
                interval_ms: int = self._settings.get("wakeIntervalMs", 300000)
                now = time.monotonic()
                if (now - self._last_wake_monotonic) * 1000.0 >= interval_ms:
                    self._last_wake_monotonic = now
                    pulse_ms:  int = self._settings.get("pulseDur",  100)
                    wake_mode: int = int(self._settings.get("wakeMode", 0))
                    self._gpio.fire_wake(pulse_ms)
                    self._send_response("WAKE_FIRED\n")
                    # Fan-out wake pulse to all paired RX modules (mirrors
                    # ESP32 runWakeTrigger() line 1313: sendEspNowWake())
                    if self._bridge and self._bridge.connected:
                        self._bridge.send(f"TX_WAKE,{pulse_ms},{wake_mode}")
            time.sleep(0.1)

    # ── Trigger sequence ──────────────────────────────────────────────────────

    def _do_trigger_sequence(self) -> None:
        """
        Mirrors ESP32 triggerCamera() + delayBeforeShotMs logic.

        AF sequence (lines 1184-1228 in firmware):
          1. Assert WAKE pin → wait afPreMs (camera focuses)
          2. Fire TRIGGER pin for each shot (with BURST_INTER_SHOT_MS gaps)
          3. Release WAKE pin after all shots

        lastTriggerTime is stamped AFTER the burst completes
        (mirrors ESP32 line 1330: "stamp after blocking pulse so cooldown
        is measured from end of burst").
        """
        # Snapshot distance at burst start — matches ESP32's tfDist which is
        # read by getData() at the top of runTriggerLogic() before any delays.
        dist = self._lidar.distance
        wake_asserted = False

        try:
            cam_mode:   int  = int(self._settings.get("camMode",   0))
            use_usb:    bool = (
                cam_mode == 6
                and self._camera is not None
                and self._camera.connected
            )
            # Direct-capture cameras (mode 7 = CSI Pi Camera, mode 8 = USB
            # webcam) share the CameraUSB trigger interface; select whichever
            # matches the active mode and is connected.
            direct_cam = None
            if cam_mode == 7 and self._camera_pi is not None and self._camera_pi.connected:
                direct_cam = self._camera_pi
            elif cam_mode == 8 and self._camera_webcam is not None and self._camera_webcam.connected:
                direct_cam = self._camera_webcam
            # USB mode has independent burst/AF settings; GPIO modes use shared settings.
            if use_usb:
                shots = max(1, min(50,
                    self._settings.get("usbBurst",
                        self._settings.get("shots", 1))))
                af_en = bool(self._settings.get("usbAfEn",
                    self._settings.get("afEnabled", False)))
            else:
                shots = max(1, min(50, self._settings.get("shots",    1)))
                af_en = bool(self._settings.get("afEnabled", False))
            delay_ms:   int  = self._settings.get("delayShot",  0)
            pulse_ms:   int  = self._settings.get("pulseDur",   300)
            af_pre_ms:  int  = self._settings.get("afPreMs",    500)
            trig_mode:  int  = int(self._settings.get("trigMode", 0))
            wake_mode:  int  = int(self._settings.get("wakeMode", 0))

            # ── Notify RX before local pre-pulse so TX and RX focus together ─
            # Mirrors ESP32 triggerCamera() line 1182: sendEspNowTrigger() is
            # called at the very top, before afPreMs, so both units start
            # focusing simultaneously and fire within ~1-2 ms of each other.
            if self._bridge and self._bridge.connected:
                self._bridge.send(f"TX_TRIGGER,{pulse_ms},{trig_mode}")

            # Optional delay before shot sequence
            if delay_ms > 0:
                time.sleep(delay_ms / 1000.0)

            # ── Assert AF pre-pulse on WAKE pin (GPIO modes only) ────────────
            # USB camera mode handles AF inside CameraUSB.trigger() via af_pre_ms.
            # Direct-capture cameras (Pi Camera / webcam) have no AF pin either.
            # Assert whenever AF is enabled — afPreMs is the hold duration only.
            if af_en and not use_usb and direct_cam is None:
                self._gpio.assert_wake()          # wakePin → active
                wake_asserted = True              # track so finally can release on exception
                if af_pre_ms > 0:
                    time.sleep(af_pre_ms / 1000.0)   # hold while camera focuses

            # ── Fire each shot ────────────────────────────────────────────────
            # USB camera mode (camMode=6): use gphoto2 trigger instead of GPIO.
            # All other modes: GPIO pulse on the trigger pin.
            if use_usb:
                # Pass af_pre_ms only for shot 0 (camera focuses then fires).
                # Burst shots after the first skip the focus delay — identical
                # semantics to the GPIO path where wake is held & re-fired.
                usb_af_ms = af_pre_ms if af_en else 0
                cam_ok = self._camera.trigger(shots=shots, af_pre_ms=usb_af_ms)
                if not cam_ok:
                    # CameraUSB.trigger() returns False when any shot fails
                    # (disconnected mid-burst, PTP error, buffer full, etc.).
                    # Notify the app so the user knows shots were missed.
                    self._send_response("CAM_ERR,TRIGGER_FAIL\n")
                # fan-out to RX modules (mirrors GPIO path below)
                for i in range(shots):
                    if i > 0 and self._bridge and self._bridge.connected:
                        self._bridge.send(f"TX_BURST,{pulse_ms},{trig_mode}")
            elif direct_cam is not None:
                # Pi Camera / webcam: capture directly to the staging dir.
                # These cameras are fixed-exposure with no AF hardware — the
                # afPreMs value acts purely as a settle delay before shot 0.
                cam_ok = direct_cam.trigger(
                    shots=shots, af_pre_ms=af_pre_ms if af_en else 0
                )
                if not cam_ok:
                    self._send_response("CAM_ERR,TRIGGER_FAIL\n")
                # fan-out to RX modules (mirrors GPIO path below)
                for i in range(shots):
                    if i > 0 and self._bridge and self._bridge.connected:
                        self._bridge.send(f"TX_BURST,{pulse_ms},{trig_mode}")
            else:
                for i in range(shots):
                    if i > 0:
                        # 200 ms inter-shot gap matches ESP32 firmware line 1221
                        time.sleep(BURST_INTER_SHOT_MS / 1000.0)
                    self._gpio.fire_trigger_blocking(pulse_ms)   # triggerPin only
                    # For shots 2+ send BURST so RX skips its AF pre-focus —
                    # mirrors ESP32 firmware line 1220: sendEspNowBurstTrigger()
                    # called for i > 0 inside the shot loop, after the GPIO pulse.
                    if i > 0 and self._bridge and self._bridge.connected:
                        self._bridge.send(f"TX_BURST,{pulse_ms},{trig_mode}")

            self._send_response(f"TRIG,{dist}\n")

        except Exception as exc:
            print(f"[Trigger] Error in burst sequence: {exc}")

        finally:
            # ── Always release wake pin if it was asserted ───────────────────
            # Prevents permanent AF/wake output assertion and _wake_lock deadlock
            # if a GPIO exception interrupts the shot sequence mid-burst.
            if wake_asserted:
                try:
                    self._gpio.release_wake()
                except Exception as exc:
                    print(f"[Trigger] Error releasing wake pin: {exc}")

            # ── Stamp after burst — matches ESP32 lines 1330-1331 ────────────
            # lastTriggerTime: cooldown measured from end of burst (line 1330)
            # lastWakeTime:    wake interval restarted from last shot (line 1331)
            now = time.monotonic()
            self._last_trigger_monotonic = now
            self._last_wake_monotonic    = now   # prevents immediate standalone wake after burst
            self._in_burst = False
