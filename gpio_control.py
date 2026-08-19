"""
GPIO output control for Pi TX trigger, wake, laser, and LED pins.

Trigger / Wake output modes:
  mode 0 (dry contact): idle = high-Z (INPUT), active = OUTPUT LOW
    Matches optocoupler / 2N3904 wiring — Pi pulls camera shutter line to GND.
  mode 1 (hot):         idle = OUTPUT LOW, active = OUTPUT HIGH (3.3 V)
    Direct 3.3 V signal to camera shutter tip.

Wake-pin ownership model (prevents AF hold from being broken by concurrent
standalone pulses — mirrors the ESP32's single-threaded main-loop guarantee):

  assert_wake()         Acquires _wake_lock (blocks until any in-progress
                        standalone pulse finishes), then drives wake pin to
                        active level. Lock is HELD until release_wake().
  release_wake()        Returns wake pin to idle, then releases _wake_lock.
  fire_wake_blocking()  Non-blocking try on _wake_lock. If a burst holds it
                        the call is silently skipped — the camera is already
                        focused; no redundant pulse needed.
  fire_wake()           Async wrapper for fire_wake_blocking.

  Sequence during an AF-enabled burst:
    assert_wake()  →  wait afPreMs  →  N × fire_trigger_blocking()  →  release_wake()

  This lock ensures no concurrent wake-timer or T_WAKE command can assert or
  release the wake pin between assert_wake() and release_wake().

Pi BCM pin defaults (all configurable in ~/.pi-tx/settings.json):
  triggerPin   17  shutter release
  wakePin      27  wake / autofocus focus hold
  laserPin     22  alignment laser EN (active HIGH), -1 to disable
  statusLedPin -1  -1 = not wired
  powerLedPin  -1  -1 = not wired
"""

import threading
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from settings import Settings

try:
    import RPi.GPIO as GPIO  # type: ignore
    _GPIO_AVAILABLE = True
except (ImportError, RuntimeError):
    _GPIO_AVAILABLE = False
    print("[GPIO] RPi.GPIO not available — output is simulated (no hardware pulses)")

# Alignment laser auto-shutoff timeout — matches ESP32 LASER_TIMEOUT_MS (300 000 ms).
# Exposed as a module constant so tests can patch it without subclassing.
LASER_TIMEOUT_SEC: float = 300.0


class GPIOControl:
    def __init__(self, settings: "Settings") -> None:
        self._settings = settings

        # Serializes concurrent trigger-pin pulses (burst vs. manual T command).
        self._trigger_lock = threading.Lock()

        # Laser auto-shutoff state
        self._laser_timer: threading.Timer | None = None

        # Owns the wake pin across assert_wake() … release_wake().
        # Standalone fire_wake*() calls do a non-blocking acquire; if the burst
        # already holds this lock they skip, preserving the AF hold.
        self._wake_lock = threading.Lock()

        if _GPIO_AVAILABLE:
            GPIO.setmode(GPIO.BCM)
            GPIO.setwarnings(False)
            self._init_pins()
        else:
            print("[GPIO] Running in stub mode")

    # ── Pin helpers ───────────────────────────────────────────────────────────

    def _pin(self, key: str) -> int:
        return int(self._settings.get(key, -1))

    def _init_pins(self) -> None:
        trig_mode = self._settings.get("trigMode", 0)
        wake_mode = self._settings.get("wakeMode", 0)
        self._setup_idle(self._pin("triggerPin"), trig_mode)
        self._setup_idle(self._pin("wakePin"),    wake_mode)
        laser_pin = self._pin("laserPin")
        if laser_pin >= 0:
            GPIO.setup(laser_pin, GPIO.OUT)
            GPIO.output(laser_pin, GPIO.LOW)
        for key in ("statusLedPin", "powerLedPin"):
            p = self._pin(key)
            if p >= 0:
                GPIO.setup(p, GPIO.OUT)
                GPIO.output(p, GPIO.LOW)

    def _setup_idle(self, pin: int, mode: int) -> None:
        """Set a trigger/wake pin to its idle (inactive) state."""
        if not _GPIO_AVAILABLE or pin < 0:
            return
        if mode == 0:   # dry contact: idle = high-Z (floating)
            GPIO.setup(pin, GPIO.IN)
        else:           # hot: idle = OUTPUT LOW
            GPIO.setup(pin, GPIO.OUT)
            GPIO.output(pin, GPIO.LOW)

    def _assert_pin(self, pin: int, mode: int, invert: bool = False) -> None:
        """Drive a pin to its active level without releasing.

        invert=True flips the active level (LOW↔HIGH) so the caller can wire
        an NPN transistor or optocoupler that needs a HIGH drive to conduct.
        """
        if not _GPIO_AVAILABLE or pin < 0:
            return
        if mode == 0:
            # Dry contact default: idle=float, active=LOW (grounds camera line directly)
            # Inverted:           idle=float, active=HIGH (for NPN/optocoupler isolation)
            GPIO.setup(pin, GPIO.OUT)
            GPIO.output(pin, GPIO.HIGH if invert else GPIO.LOW)
        else:
            # Hot 3.3V default:  idle=LOW, active=HIGH
            # Inverted:          idle=LOW, active=LOW (already at idle — effectively no-op)
            GPIO.setup(pin, GPIO.OUT)
            GPIO.output(pin, GPIO.LOW if invert else GPIO.HIGH)

    def _release_pin(self, pin: int, mode: int, invert: bool = False) -> None:
        """Return a pin to idle after it was asserted."""
        if not _GPIO_AVAILABLE or pin < 0:
            return
        if mode == 0:
            # Dry contact default: idle = float (high-Z)
            # Inverted:            idle = float (same — float is always the safe idle)
            GPIO.setup(pin, GPIO.IN)
        else:
            # Hot mode: idle = LOW regardless of inversion
            GPIO.output(pin, GPIO.LOW)

    # ── Output mode configuration ─────────────────────────────────────────────

    def apply_trig_mode(self, mode: int) -> None:
        self._setup_idle(self._pin("triggerPin"), mode)

    def apply_wake_mode(self, mode: int) -> None:
        self._setup_idle(self._pin("wakePin"), mode)

    # ── Laser / LED ───────────────────────────────────────────────────────────

    def set_laser(self, on: bool) -> None:
        """Turn alignment laser on or off.

        Mirrors ESP32 LASER,1 / LASER,0 behavior:
          ON:  start / reset a LASER_TIMEOUT_SEC auto-shutoff timer.
               Repeated LASER,1 commands reset the timer, same as ESP32.
          OFF: cancel the timer immediately and drive pin low.
        """
        # Always cancel any existing timer first (reset on repeated ON, or cancel on OFF)
        if self._laser_timer is not None:
            self._laser_timer.cancel()
            self._laser_timer = None

        if on:
            # Schedule auto-shutoff after LASER_TIMEOUT_SEC (module constant, patchable)
            t = threading.Timer(LASER_TIMEOUT_SEC, self._laser_auto_off)
            t.daemon = True
            t.start()
            self._laser_timer = t

        pin = self._pin("laserPin")
        if _GPIO_AVAILABLE and pin >= 0:
            GPIO.output(pin, GPIO.HIGH if on else GPIO.LOW)
        else:
            print(f"[GPIO] Laser {'ON' if on else 'OFF'} (simulated)")

    def _laser_auto_off(self) -> None:
        """Called by the auto-shutoff timer after LASER_TIMEOUT_SEC seconds."""
        self._laser_timer = None
        pin = self._pin("laserPin")
        if _GPIO_AVAILABLE and pin >= 0:
            GPIO.output(pin, GPIO.LOW)
        else:
            print("[GPIO] Laser auto-shutoff (simulated)")
        print("[LASER] Auto-shutoff (5 min)")

    def set_status_led(self, on: bool) -> None:
        pin = self._pin("statusLedPin")
        if _GPIO_AVAILABLE and pin >= 0:
            GPIO.output(pin, GPIO.HIGH if on else GPIO.LOW)

    def set_power_led(self, on: bool) -> None:
        pin = self._pin("powerLedPin")
        if _GPIO_AVAILABLE and pin >= 0:
            GPIO.output(pin, GPIO.HIGH if on else GPIO.LOW)

    # ── Wake-pin AF hold (burst ownership) ───────────────────────────────────

    def assert_wake(self) -> None:
        """Acquire wake-pin ownership and drive to active level.

        Blocks until any in-progress standalone wake pulse (fire_wake*) finishes,
        then asserts the wake pin. The caller MUST call release_wake() afterward.
        Holding _wake_lock prevents any concurrent fire_wake*() call from
        disturbing the pin for the lifetime of the AF burst.

        If the GPIO assertion itself raises, the lock is released and the
        exception is re-raised so the caller's wake_asserted flag stays False
        and release_wake() is not called.
        """
        self._wake_lock.acquire()   # blocking — waits for any standalone pulse
        try:
            pin  = self._pin("wakePin")
            mode = int(self._settings.get("wakeMode", 0))
            if not _GPIO_AVAILABLE:
                print(f"[GPIO] AF assert_wake  pin={pin}  mode={mode} (simulated)")
            # _assert_pin is a no-op when GPIO unavailable; always called so
            # tests can patch it to inject failures even in stub mode.
            self._assert_pin(pin, mode)
        except Exception:
            # Rollback: release the lock so future callers are not blocked
            self._wake_lock.release()
            raise

    def release_wake(self) -> None:
        """Return wake pin to idle and release wake-pin ownership.

        The lock is always released in a finally block even if _release_pin
        raises a GPIO error, so the lock can never remain permanently held.
        """
        try:
            pin  = self._pin("wakePin")
            mode = int(self._settings.get("wakeMode", 0))
            if not _GPIO_AVAILABLE:
                print(f"[GPIO] AF release_wake pin={pin}  mode={mode} (simulated)")
            # _release_pin is a no-op when GPIO unavailable; always called so
            # tests can patch it to inject failures even in stub mode.
            self._release_pin(pin, mode)
        finally:
            self._wake_lock.release()

    # ── Trigger shutter (TRIGGER pin only — AF handled above) ────────────────

    def fire_trigger(self, pulse_ms: int) -> None:
        """Fire shutter trigger asynchronously. No AF — caller handles wake pin."""
        threading.Thread(
            target=self.fire_trigger_blocking,
            args=(pulse_ms,),
            daemon=True,
        ).start()

    def fire_trigger_blocking(self, pulse_ms: int) -> None:
        """Fire shutter trigger synchronously — blocks until pulse completes.
        Serialised via _trigger_lock so burst shots can't overlap manual T fires.
        Applies invertTrig polarity inversion when the setting is enabled.
        """
        pin    = self._pin("triggerPin")
        mode   = int(self._settings.get("trigMode",   0))
        invert = bool(self._settings.get("invertTrig", False))
        with self._trigger_lock:
            self._do_pulse(pin, mode, pulse_ms, invert)

    # ── Wake pulse (standalone — periodic timer or T_WAKE command) ────────────

    def fire_wake(self, pulse_ms: int = 100) -> None:
        """Fire a standalone wake pulse asynchronously.
        Silently skipped if a burst currently holds wake-pin ownership.
        """
        threading.Thread(
            target=self.fire_wake_blocking,
            args=(pulse_ms,),
            daemon=True,
        ).start()

    def fire_wake_blocking(self, pulse_ms: int) -> None:
        """Fire a standalone wake pulse synchronously.
        Non-blocking acquire: if a burst owns the wake pin (AF hold in progress)
        this call is skipped — the camera is already focused/awake.
        """
        if not self._wake_lock.acquire(blocking=False):
            # Burst owns the wake pin; standalone pulse would break the AF hold.
            print("[GPIO] fire_wake_blocking skipped — burst holds wake pin")
            return
        try:
            pin  = self._pin("wakePin")
            mode = int(self._settings.get("wakeMode", 0))
            self._do_pulse(pin, mode, pulse_ms)
        finally:
            self._wake_lock.release()

    # ── Cleanup ───────────────────────────────────────────────────────────────

    def cleanup(self) -> None:
        if self._laser_timer is not None:
            self._laser_timer.cancel()
            self._laser_timer = None
        if _GPIO_AVAILABLE:
            try:
                GPIO.cleanup()
                print("[GPIO] Cleaned up")
            except Exception:
                pass

    # ── Internal ──────────────────────────────────────────────────────────────

    def _do_pulse(self, pin: int, mode: int, pulse_ms: int, invert: bool = False) -> None:
        """Assert pin for pulse_ms then return to idle (no locks held here).

        invert passes through to _assert_pin/_release_pin for invertTrig support.

        _release_pin is called in a finally block so the pin is always returned
        to idle even if an exception (e.g. thread interruption) occurs mid-sleep.
        Release failures are logged as best-effort; the original exception propagates.
        """
        if not _GPIO_AVAILABLE:
            polarity = "HIGH" if (invert ^ (mode == 1)) else "LOW"
            print(f"[GPIO] Simulated pulse  pin={pin}  mode={mode}  invert={invert}"
                  f"  active={polarity}  pulse={pulse_ms}ms")
            time.sleep(pulse_ms / 1000.0)
            return
        if pin < 0:
            return
        self._assert_pin(pin, mode, invert)
        try:
            time.sleep(pulse_ms / 1000.0)
        finally:
            try:
                self._release_pin(pin, mode, invert)
            except Exception as exc:
                print(f"[GPIO] Error releasing pin {pin} after pulse: {exc}")
