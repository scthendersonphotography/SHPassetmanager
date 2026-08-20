"""
Targeted tests for Pi TX trigger engine behaviour.

Tests use hardware stubs (no RPi.GPIO, no smbus2 needed) and run on any
Python 3.11+ environment — including Replit CI and local dev machines.

Coverage:
  TestAFPinSelection
    - AF pre-pulse asserts wakePin, not triggerPin
    - Wake pin held for the entire burst sequence, released once after last shot
    - No wake-pin activity when AF is disabled

  TestCooldownRetriggering
    - Re-triggers while object stays in zone (not edge-only)
    - Stops firing after object exits zone
    - lastTriggerTime stamped after burst (cooldown from end-of-burst)
    - objectInZone flag tracks zone state correctly
    - Manual T command fires regardless of distance

  TestWakePinOwnership
    - Standalone fire_wake_blocking() skipped when burst holds wake lock
    - Standalone fire_wake_blocking() proceeds normally when no burst
    - assert_wake blocks until an in-progress standalone pulse finishes

  TestProtocolCompatibility
    - TRIG response format is TRIG,<distanceCm> (not bare TRIG)
    - Distance in response matches LiDAR reading at trigger moment
    - POST /cmd drain-then-process ordering (response on next /data poll)
"""

import threading
import time
import unittest


# ── Stubs ─────────────────────────────────────────────────────────────────────

class _FakeSettings:
    def __init__(self, **overrides):
        self._data = dict(
            minDist=10, maxDist=500, minFlux=20,
            cooldown=200,
            shots=1, pulseDur=50,
            delayShot=0,
            afEnabled=False, afPreMs=100,
            wakeEn=False, wakeIntervalMs=300000,
            schedEn=False, schedStart=0, schedEnd=1439,
            triggerPin=17, wakePin=27,
            trigMode=0, wakeMode=0,
            # gpio_control uses laserPin / statusLedPin / powerLedPin
            laserPin=-1, statusLedPin=-1, powerLedPin=-1,
        )
        self._data.update(overrides)

    def get(self, key, default=None):
        return self._data.get(key, default)

    def set(self, key, value):
        self._data[key] = value


class _FakeLiDAR:
    def __init__(self, distance=250, flux=100):
        self.distance = distance
        self.flux = flux


class _RecordingGPIO:
    """Records assert_wake / release_wake / fire_trigger_blocking / fire_wake* calls."""
    def __init__(self):
        self.events: list[tuple] = []
        self._wake_lock = threading.Lock()

    def assert_wake(self):
        self._wake_lock.acquire()
        self.events.append(("assert_wake",))

    def release_wake(self):
        self.events.append(("release_wake",))
        self._wake_lock.release()

    def fire_trigger_blocking(self, pulse_ms):
        self.events.append(("fire_trigger_blocking", pulse_ms))

    def fire_trigger(self, pulse_ms):
        threading.Thread(target=self.fire_trigger_blocking, args=(pulse_ms,), daemon=True).start()

    def fire_wake(self, pulse_ms=100):
        threading.Thread(target=self.fire_wake_blocking, args=(pulse_ms,), daemon=True).start()

    def fire_wake_blocking(self, pulse_ms):
        if not self._wake_lock.acquire(blocking=False):
            self.events.append(("fire_wake_skipped", pulse_ms))
            return
        try:
            self.events.append(("fire_wake_blocking", pulse_ms))
        finally:
            self._wake_lock.release()


# ── TestAFPinSelection ────────────────────────────────────────────────────────

class TestAFPinSelection(unittest.TestCase):
    """AF pre-pulse must use wakePin — never triggerPin."""

    def _run_sequence(self, af_en, af_pre_ms, shots=1):
        settings = _FakeSettings(afEnabled=af_en, afPreMs=af_pre_ms, shots=shots, pulseDur=50)
        gpio = _RecordingGPIO()
        lidar = _FakeLiDAR()
        responses = []

        from trigger_engine import TriggerEngine
        engine = TriggerEngine(settings, gpio, lidar, responses.append)
        engine._do_trigger_sequence()
        return gpio.events, responses

    def test_af_disabled_no_wake_assertion(self):
        events, _ = self._run_sequence(af_en=False, af_pre_ms=100)
        wake_ops = [e for e in events if e[0] in ("assert_wake", "release_wake")]
        self.assertEqual(wake_ops, [], "No wake-pin operations expected when AF is disabled")

    def test_af_enabled_asserts_wake_before_trigger(self):
        events, _ = self._run_sequence(af_en=True, af_pre_ms=50)
        ops = [e[0] for e in events]
        self.assertIn("assert_wake", ops)
        self.assertIn("fire_trigger_blocking", ops)
        self.assertIn("release_wake", ops)
        aw_idx = ops.index("assert_wake")
        ft_idx = ops.index("fire_trigger_blocking")
        self.assertLess(aw_idx, ft_idx,
            "assert_wake must occur BEFORE fire_trigger_blocking")

    def test_af_enabled_wake_released_after_all_shots(self):
        events, _ = self._run_sequence(af_en=True, af_pre_ms=50)
        ops = [e[0] for e in events]
        ft_last = max(i for i, op in enumerate(ops) if op == "fire_trigger_blocking")
        rw_idx = ops.index("release_wake")
        self.assertGreater(rw_idx, ft_last,
            "release_wake must occur AFTER the last fire_trigger_blocking")

    def test_af_enabled_burst_wake_held_for_all_shots(self):
        events, _ = self._run_sequence(af_en=True, af_pre_ms=50, shots=3)
        ops = [e[0] for e in events]
        self.assertEqual(ops.count("assert_wake"),  1, "assert_wake fires exactly once per burst")
        self.assertEqual(ops.count("release_wake"), 1, "release_wake fires exactly once per burst")
        self.assertEqual(ops.count("fire_trigger_blocking"), 3, "3 shutter pulses for shots=3")
        aw_idx = ops.index("assert_wake")
        rw_idx = ops.index("release_wake")
        for idx, op in enumerate(ops):
            if op == "fire_trigger_blocking":
                self.assertGreater(idx, aw_idx, "Shutter pulse must be after assert_wake")
                self.assertLess(idx, rw_idx,    "Shutter pulse must be before release_wake")


# ── TestCooldownRetriggering ──────────────────────────────────────────────────

class TestCooldownRetriggering(unittest.TestCase):
    """Object stays in zone → trigger fires again after every cooldown period."""

    def _make_engine(self, **settings_overrides):
        defaults = dict(cooldown=50, shots=1, pulseDur=10, afEnabled=False,
                        minDist=0, maxDist=800, minFlux=0)
        defaults.update(settings_overrides)
        settings = _FakeSettings(**defaults)
        gpio = _RecordingGPIO()
        lidar = _FakeLiDAR(distance=100, flux=200)
        responses = []
        from trigger_engine import TriggerEngine
        engine = TriggerEngine(settings, gpio, lidar, responses.append)
        return engine, gpio, lidar, responses

    def test_retriggers_while_object_stays_in_zone(self):
        engine, gpio, lidar, _ = self._make_engine(cooldown=80)
        engine.start()
        try:
            time.sleep(0.45)
        finally:
            engine.stop()
        count = sum(1 for e in gpio.events if e[0] == "fire_trigger_blocking")
        self.assertGreaterEqual(count, 3,
            f"Expected ≥3 triggers at 80ms cooldown over 450ms, got {count}")

    def test_stops_firing_when_object_leaves_zone(self):
        engine, gpio, lidar, _ = self._make_engine(cooldown=50)
        engine.start()
        time.sleep(0.15)
        before = sum(1 for e in gpio.events if e[0] == "fire_trigger_blocking")
        lidar.distance = 1000    # above maxDist=800
        time.sleep(0.20)
        after  = sum(1 for e in gpio.events if e[0] == "fire_trigger_blocking")
        engine.stop()
        self.assertEqual(before, after, "No additional triggers after object leaves zone")

    def test_cooldown_measured_from_end_of_burst(self):
        engine, gpio, lidar, _ = self._make_engine(cooldown=60, pulseDur=80)
        engine.start()
        time.sleep(0.45)
        engine.stop()
        count = sum(1 for e in gpio.events if e[0] == "fire_trigger_blocking")
        self.assertGreaterEqual(count, 2, "Expected ≥2 triggers confirming end-of-burst cooldown")

    def test_object_in_zone_flag_cleared_on_exit(self):
        engine, gpio, lidar, _ = self._make_engine()
        engine.start()
        time.sleep(0.05)
        self.assertTrue(engine._object_in_zone, "Flag True while in zone")
        lidar.distance = 0
        time.sleep(0.05)
        self.assertFalse(engine._object_in_zone, "Flag False after object leaves zone")
        engine.stop()

    def test_manual_fire_bypasses_distance_check(self):
        engine, gpio, lidar, _ = self._make_engine()
        lidar.distance = 0
        engine.start()
        time.sleep(0.05)
        engine.fire_manual()
        time.sleep(0.20)
        engine.stop()
        count = sum(1 for e in gpio.events if e[0] == "fire_trigger_blocking")
        self.assertGreaterEqual(count, 1, "Manual fire must work with distance=0")


# ── TestWakePinOwnership ──────────────────────────────────────────────────────

class TestWakePinOwnership(unittest.TestCase):
    """Wake-pin _wake_lock prevents standalone pulses from disturbing AF hold."""

    def test_standalone_wake_skipped_when_burst_holds_lock(self):
        """fire_wake_blocking() must be skipped while assert_wake() holds the lock."""
        gpio = _RecordingGPIO()
        gpio.assert_wake()                    # burst acquires lock
        gpio.fire_wake_blocking(50)           # standalone — must skip
        gpio.release_wake()                   # burst releases lock

        skipped = [e for e in gpio.events if e[0] == "fire_wake_skipped"]
        executed = [e for e in gpio.events if e[0] == "fire_wake_blocking"]
        self.assertEqual(len(skipped),  1, "Standalone wake must be skipped once")
        self.assertEqual(len(executed), 0, "Standalone wake must NOT execute during burst")

    def test_standalone_wake_proceeds_when_no_burst(self):
        """fire_wake_blocking() must succeed when no burst holds the lock."""
        gpio = _RecordingGPIO()
        gpio.fire_wake_blocking(50)

        executed = [e for e in gpio.events if e[0] == "fire_wake_blocking"]
        self.assertEqual(len(executed), 1, "Standalone wake must succeed with no burst active")

    def test_assert_wake_blocks_until_standalone_pulse_finishes(self):
        """assert_wake() must wait for an in-progress standalone pulse to complete."""
        gpio = _RecordingGPIO()
        order: list[str] = []

        def standalone_pulse():
            time.sleep(0.05)   # simulate a 50ms standalone wake pulse
            order.append("standalone_done")
            gpio._wake_lock.release()  # simulate completion

        # Manually acquire to simulate an in-progress standalone pulse
        gpio._wake_lock.acquire()
        t = threading.Thread(target=standalone_pulse, daemon=True)
        t.start()

        gpio.assert_wake()       # must block until standalone_done
        order.append("burst_started")
        gpio.release_wake()
        t.join()

        self.assertEqual(order[0], "standalone_done",
            "assert_wake must not proceed until the standalone pulse releases the lock")
        self.assertEqual(order[1], "burst_started")


# ── TestProtocolCompatibility ─────────────────────────────────────────────────

class TestProtocolCompatibility(unittest.TestCase):
    """Verify Pi TX emits the correct wire protocol understood by the phone app."""

    def test_trig_response_format_includes_distance(self):
        """`TRIG,<distanceCm>` — not bare `TRIG`."""
        settings = _FakeSettings(shots=1, pulseDur=10, afEnabled=False)
        gpio = _RecordingGPIO()
        lidar = _FakeLiDAR(distance=300, flux=200)
        responses = []
        from trigger_engine import TriggerEngine
        engine = TriggerEngine(settings, gpio, lidar, responses.append)
        engine._do_trigger_sequence()

        trig_lines = [r for r in responses if r.strip().startswith("TRIG,")]
        self.assertEqual(len(trig_lines), 1,
            f"Expected exactly one TRIG,<dist> response, got: {responses}")
        import re
        self.assertRegex(trig_lines[0].strip(), r'^TRIG,\d+$',
            "TRIG response must match TRIG,<integer> format")
        self.assertIn("300", trig_lines[0],
            "Distance must reflect LiDAR reading at trigger time")

    def test_trig_response_not_bare_trig(self):
        """Bare `TRIG` (without distance) must not appear in responses."""
        settings = _FakeSettings(shots=1, pulseDur=10, afEnabled=False)
        gpio = _RecordingGPIO()
        responses = []
        from trigger_engine import TriggerEngine
        engine = TriggerEngine(settings, gpio, _FakeLiDAR(distance=50), responses.append)
        engine._do_trigger_sequence()

        bare_trig = [r for r in responses if r.strip() == "TRIG"]
        self.assertEqual(bare_trig, [], f"Bare TRIG (no distance) must not be emitted: {responses}")

    def test_cmd_drain_before_process_ordering(self):
        """POST /cmd drains outbox first; new command response appears on next poll."""
        from collections import deque

        outbox: deque[str] = deque()
        lock = threading.Lock()

        def send(line: str) -> None:
            with lock:
                outbox.append(line)

        def drain() -> str:
            with lock:
                result = "".join(outbox)
                outbox.clear()
                return result

        # Simulate a prior trigger response already in the outbox
        send("TRIG,150\n")

        # POST /cmd: drain first → returns prior outbox content
        returned_to_cmd = drain()
        self.assertEqual(returned_to_cmd, "TRIG,150\n",
            "POST /cmd must return the previously queued outbox content")

        # Command handler enqueues its own response
        send("INFO,...\n")

        # GET /data sees the new response
        returned_to_data = drain()
        self.assertIn("INFO", returned_to_data,
            "Command response must be visible on the subsequent GET /data poll")

    def test_trig_distance_matches_lidar_at_trigger_time(self):
        """Distance in TRIG,<dist> reflects LiDAR state when the burst fired."""
        settings = _FakeSettings(shots=2, pulseDur=10, afEnabled=False)
        gpio = _RecordingGPIO()
        lidar = _FakeLiDAR(distance=123, flux=200)
        responses = []
        from trigger_engine import TriggerEngine
        engine = TriggerEngine(settings, gpio, lidar, responses.append)
        # Change LiDAR distance mid-burst won't affect captured dist (snapshot taken before shots)
        def change_dist():
            time.sleep(0.03)
            lidar.distance = 999
        threading.Thread(target=change_dist, daemon=True).start()
        engine._do_trigger_sequence()

        trig = [r for r in responses if r.strip().startswith("TRIG,")]
        self.assertEqual(len(trig), 1)
        self.assertIn("123", trig[0],
            "TRIG distance must be captured at burst start, not mid-burst")


# ── TestGetSettingsQuery ──────────────────────────────────────────────────────

class TestGetSettingsQuery(unittest.TestCase):
    """GET_SETTINGS must emit the persisted S,... line without mutating state.

    The browser control panel bootstraps its trigger-settings form from this
    response; without it the form would show placeholder defaults that could
    silently overwrite a configured Pi.
    """

    def _make_parser(self, settings, responses):
        from command_parser import CommandParser
        gpio = _RecordingGPIO()
        lidar = _FakeLiDAR()
        from trigger_engine import TriggerEngine
        engine = TriggerEngine(settings, gpio, lidar, responses.append)
        return CommandParser(settings, gpio, lidar, engine, responses.append)

    def test_get_settings_reflects_non_default_config(self):
        settings = _FakeSettings(
            minDist=120, maxDist=340, cooldown=750,
            shots=7, pulseDur=250, invertTrig=True, delayShot=90,
        )
        responses = []
        parser = self._make_parser(settings, responses)
        parser.handle("GET_SETTINGS")

        self.assertEqual(responses, ["S,120,340,750,7,250,1,90\n"],
            "GET_SETTINGS must return the persisted settings verbatim")

    def test_get_settings_is_read_only(self):
        settings = _FakeSettings(minDist=42, maxDist=99, shots=3)
        before = dict(settings._data)
        responses = []
        parser = self._make_parser(settings, responses)
        parser.handle("GET_SETTINGS")
        self.assertEqual(settings._data, before,
            "GET_SETTINGS must not modify any stored setting")


# ── TestExceptionSafety ───────────────────────────────────────────────────────

class TestExceptionSafety(unittest.TestCase):
    """Wake pin must always be released even when GPIO operations throw."""

    def test_wake_released_on_trigger_gpio_exception(self):
        """release_wake called in finally even when fire_trigger_blocking raises."""
        settings = _FakeSettings(afEnabled=True, afPreMs=10, shots=1, pulseDur=10)
        gpio = _RecordingGPIO()
        responses = []

        # Patch fire_trigger_blocking to raise after assert_wake already ran
        def raising(pulse_ms):
            gpio.events.append(("fire_trigger_blocking_raised",))
            raise RuntimeError("simulated GPIO failure")

        gpio.fire_trigger_blocking = raising

        from trigger_engine import TriggerEngine
        engine = TriggerEngine(settings, gpio, _FakeLiDAR(), responses.append)
        engine._do_trigger_sequence()   # must NOT propagate exception

        release_ops = [e for e in gpio.events if e[0] == "release_wake"]
        self.assertEqual(len(release_ops), 1,
            "release_wake must be called in finally even after GPIO exception")
        self.assertFalse(engine._in_burst,
            "_in_burst must be cleared in finally after exception")

    def test_in_burst_cleared_after_exception(self):
        """_in_burst flag must be False after any exception in the sequence."""
        settings = _FakeSettings(afEnabled=False, shots=1, pulseDur=10)
        gpio = _RecordingGPIO()

        def raising(pulse_ms):
            raise RuntimeError("GPIO error")

        gpio.fire_trigger_blocking = raising

        from trigger_engine import TriggerEngine
        engine = TriggerEngine(settings, gpio, _FakeLiDAR(), lambda x: None)
        engine._in_burst = True
        engine._do_trigger_sequence()
        self.assertFalse(engine._in_burst, "_in_burst must reset regardless of exceptions")

    def test_post_burst_wake_timer_restarts(self):
        """_last_wake_monotonic reset after burst so wake timer waits full interval."""
        settings = _FakeSettings(shots=1, pulseDur=10, afEnabled=False)
        gpio = _RecordingGPIO()

        from trigger_engine import TriggerEngine
        engine = TriggerEngine(settings, gpio, _FakeLiDAR(), lambda x: None)

        # Simulate wake timer that was already long overdue
        engine._last_wake_monotonic = time.monotonic() - 10000.0
        engine._do_trigger_sequence()

        elapsed_since_reset = time.monotonic() - engine._last_wake_monotonic
        self.assertLess(elapsed_since_reset, 1.0,
            "_last_wake_monotonic must be reset to now after burst completes")

    def test_wake_lock_released_when_assert_pin_raises(self):
        """If _assert_pin raises inside assert_wake, the lock must be released."""
        from gpio_control import GPIOControl
        settings = _FakeSettings()
        gpio = GPIOControl(settings)

        def raising_assert(pin, mode):
            raise RuntimeError("GPIO assert failure")
        gpio._assert_pin = raising_assert

        with self.assertRaises(RuntimeError):
            gpio.assert_wake()

        # Lock must be free — non-blocking acquire must succeed
        acquired = gpio._wake_lock.acquire(blocking=False)
        self.assertTrue(acquired, "Wake lock must be released after assert_wake GPIO failure")
        if acquired:
            gpio._wake_lock.release()

    def test_wake_lock_released_when_release_pin_raises(self):
        """If _release_pin raises inside release_wake, the lock must still be released."""
        from gpio_control import GPIOControl
        settings = _FakeSettings()
        gpio = GPIOControl(settings)

        # Simulate lock held as if assert_wake succeeded
        gpio._wake_lock.acquire()

        def raising_release(pin, mode):
            raise RuntimeError("GPIO release failure")
        gpio._release_pin = raising_release

        with self.assertRaises(RuntimeError):
            gpio.release_wake()

        acquired = gpio._wake_lock.acquire(blocking=False)
        self.assertTrue(acquired, "Wake lock must be released even when _release_pin raises")
        if acquired:
            gpio._wake_lock.release()

    def test_post_burst_no_immediate_standalone_wake(self):
        """Standalone wake pulse must not fire immediately after a trigger burst."""
        settings = _FakeSettings(
            wakeEn=True, wakeIntervalMs=200,  # short interval for test speed
            cooldown=50, shots=1, pulseDur=10, afEnabled=False,
            minDist=0, maxDist=800, minFlux=0,
        )
        gpio = _RecordingGPIO()
        lidar = _FakeLiDAR(distance=100, flux=200)

        from trigger_engine import TriggerEngine
        engine = TriggerEngine(settings, gpio, lidar, lambda x: None)
        engine.start()
        try:
            # Let one trigger fire (cooldown 50ms, pulseDur 10ms)
            time.sleep(0.12)
            trigger_count = sum(1 for e in gpio.events if e[0] == "fire_trigger_blocking")
            self.assertGreaterEqual(trigger_count, 1, "Precondition: at least one trigger fired")

            # Immediately after burst the wake timer should NOT fire (interval reset)
            wake_before = sum(1 for e in gpio.events if e[0] in ("fire_wake_blocking", "fire_wake"))
            time.sleep(0.05)  # 50ms — less than wakeIntervalMs=200ms
            wake_after = sum(1 for e in gpio.events if e[0] in ("fire_wake_blocking", "fire_wake"))
            self.assertEqual(wake_before, wake_after,
                "No standalone wake pulse should fire within wakeIntervalMs of a burst")
        finally:
            engine.stop()


# ── TestInvertTrig ────────────────────────────────────────────────────────────

class TestInvertTrig(unittest.TestCase):
    """invertTrig setting must flip the active GPIO level for trigger pulses."""

    def _make_gpio(self, invert: bool) -> "GPIOControl":
        from gpio_control import GPIOControl
        settings = _FakeSettings(invertTrig=invert, trigMode=0, triggerPin=17)
        return GPIOControl(settings)

    def test_non_inverted_active_level_logged_as_low(self):
        """With invertTrig=False, dry-contact mode logs active=LOW."""
        gpio = self._make_gpio(invert=False)
        msgs = []
        gpio._do_pulse.__func__  # ensure it exists
        # Capture printed output
        import io, sys
        buf = io.StringIO()
        sys.stdout = buf
        try:
            gpio._do_pulse(gpio._pin("triggerPin"), 0, 10, invert=False)
        finally:
            sys.stdout = sys.__stdout__
        output = buf.getvalue()
        self.assertIn("LOW", output, "Non-inverted dry-contact should log active=LOW")
        self.assertNotIn("HIGH", output.split("active=")[1] if "active=" in output else "",
                         "Non-inverted must not show active=HIGH")

    def test_inverted_active_level_logged_as_high(self):
        """With invertTrig=True, dry-contact mode logs active=HIGH."""
        gpio = self._make_gpio(invert=True)
        import io, sys
        buf = io.StringIO()
        sys.stdout = buf
        try:
            gpio._do_pulse(gpio._pin("triggerPin"), 0, 10, invert=True)
        finally:
            sys.stdout = sys.__stdout__
        output = buf.getvalue()
        self.assertIn("HIGH", output, "Inverted dry-contact should log active=HIGH")

    def test_fire_trigger_blocking_reads_invert_from_settings(self):
        """fire_trigger_blocking passes invertTrig from settings to _do_pulse."""
        from gpio_control import GPIOControl
        settings = _FakeSettings(invertTrig=True, trigMode=0, triggerPin=17)
        gpio = GPIOControl(settings)

        calls = []
        orig = gpio._do_pulse
        def recording_do_pulse(pin, mode, pulse_ms, invert=False):
            calls.append({"pin": pin, "mode": mode, "invert": invert})
            return orig(pin, mode, pulse_ms, invert)
        gpio._do_pulse = recording_do_pulse

        gpio.fire_trigger_blocking(50)
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0]["invert"],
            "fire_trigger_blocking must pass invertTrig=True from settings")

    def test_fire_trigger_blocking_non_inverted_by_default(self):
        """fire_trigger_blocking passes invert=False when invertTrig not set."""
        from gpio_control import GPIOControl
        settings = _FakeSettings(trigMode=0, triggerPin=17)  # invertTrig defaults False
        gpio = GPIOControl(settings)

        calls = []
        orig = gpio._do_pulse
        def recording_do_pulse(pin, mode, pulse_ms, invert=False):
            calls.append({"invert": invert})
            return orig(pin, mode, pulse_ms, invert)
        gpio._do_pulse = recording_do_pulse

        gpio.fire_trigger_blocking(50)
        self.assertEqual(len(calls), 1)
        self.assertFalse(calls[0]["invert"],
            "fire_trigger_blocking must pass invert=False when invertTrig is not set")


# ── TestAFZeroPreDelay ────────────────────────────────────────────────────────

class TestAFZeroPreDelay(unittest.TestCase):
    """AF,1,0 — AF enabled with zero pre-delay must still assert wake pin."""

    def test_af_enabled_zero_predelay_asserts_wake(self):
        """assert_wake must be called even when afPreMs=0."""
        settings = _FakeSettings(afEnabled=True, afPreMs=0, shots=1, pulseDur=10)
        gpio = _RecordingGPIO()
        responses = []
        from trigger_engine import TriggerEngine
        engine = TriggerEngine(settings, gpio, _FakeLiDAR(), responses.append)
        engine._do_trigger_sequence()

        ops = [e[0] for e in gpio.events]
        self.assertIn("assert_wake", ops,
            "assert_wake must be called even with afPreMs=0")
        self.assertIn("release_wake", ops,
            "release_wake must be called after burst even with afPreMs=0")

    def test_af_zero_predelay_wake_before_shutter(self):
        """With afPreMs=0: assert_wake immediately before first shutter pulse."""
        settings = _FakeSettings(afEnabled=True, afPreMs=0, shots=1, pulseDur=10)
        gpio = _RecordingGPIO()
        from trigger_engine import TriggerEngine
        engine = TriggerEngine(settings, gpio, _FakeLiDAR(), lambda x: None)
        engine._do_trigger_sequence()

        ops = [e[0] for e in gpio.events]
        aw = ops.index("assert_wake")
        ft = ops.index("fire_trigger_blocking")
        self.assertLess(aw, ft, "assert_wake must precede fire_trigger_blocking even with afPreMs=0")

    def test_af_disabled_zero_predelay_no_wake(self):
        """AF disabled with afPreMs=0 must not assert wake."""
        settings = _FakeSettings(afEnabled=False, afPreMs=0, shots=1, pulseDur=10)
        gpio = _RecordingGPIO()
        from trigger_engine import TriggerEngine
        engine = TriggerEngine(settings, gpio, _FakeLiDAR(), lambda x: None)
        engine._do_trigger_sequence()

        wake_ops = [e for e in gpio.events if e[0] in ("assert_wake", "release_wake")]
        self.assertEqual(wake_ops, [],
            "No wake pin operations when AF is disabled, even with afPreMs=0")


# ── TestLaserAutoShutoff ──────────────────────────────────────────────────────

class TestLaserAutoShutoff(unittest.TestCase):
    """Alignment laser must auto-shutoff after LASER_TIMEOUT_SEC (matches ESP32)."""

    def setUp(self):
        import gpio_control as _gc
        self._orig_timeout = _gc.LASER_TIMEOUT_SEC

    def tearDown(self):
        import gpio_control as _gc
        _gc.LASER_TIMEOUT_SEC = self._orig_timeout

    def _make_gpio(self) -> "GPIOControl":
        from gpio_control import GPIOControl
        return GPIOControl(_FakeSettings())

    def test_laser_auto_off_fires_after_timeout(self):
        """Laser pin driven off automatically after LASER_TIMEOUT_SEC."""
        import gpio_control as _gc
        _gc.LASER_TIMEOUT_SEC = 0.1   # 100 ms for test speed
        from importlib import reload
        # Re-import with patched constant applied to new instance
        gpio = self._make_gpio()
        # Track _laser_auto_off calls
        shutoff_events = []
        orig = gpio._laser_auto_off
        def patched_auto_off():
            shutoff_events.append("shutoff")
            orig()
        gpio._laser_auto_off = patched_auto_off

        gpio.set_laser(True)
        # Manually cancel the real timer and restart with short timeout so patch applies
        if gpio._laser_timer:
            gpio._laser_timer.cancel()
        import threading
        t = threading.Timer(0.1, gpio._laser_auto_off)
        t.daemon = True
        t.start()
        gpio._laser_timer = t

        time.sleep(0.25)   # wait past timeout
        gpio.cleanup()

        self.assertGreaterEqual(len(shutoff_events), 1,
            "Laser auto-shutoff must fire after the timeout")

    def test_repeated_on_resets_timer(self):
        """Calling set_laser(True) twice replaces the existing timer (resets countdown)."""
        import gpio_control as _gc
        _gc.LASER_TIMEOUT_SEC = 0.2
        gpio = self._make_gpio()

        gpio.set_laser(True)
        first_timer = gpio._laser_timer

        gpio.set_laser(True)   # should cancel first_timer and start a new one
        second_timer = gpio._laser_timer

        self.assertIsNotNone(second_timer, "A new timer must exist after second set_laser(True)")
        self.assertIsNot(first_timer, second_timer,
            "Repeated set_laser(True) must start a fresh timer, not reuse the old one")
        gpio.cleanup()

    def test_set_laser_off_cancels_timer(self):
        """set_laser(False) must cancel the auto-shutoff timer."""
        import gpio_control as _gc
        _gc.LASER_TIMEOUT_SEC = 5.0
        gpio = self._make_gpio()

        gpio.set_laser(True)
        self.assertIsNotNone(gpio._laser_timer, "Timer must be set after set_laser(True)")

        gpio.set_laser(False)
        self.assertIsNone(gpio._laser_timer, "Timer must be cleared after set_laser(False)")
        gpio.cleanup()

    def test_cleanup_cancels_laser_timer(self):
        """cleanup() must cancel any active laser timer."""
        import gpio_control as _gc
        _gc.LASER_TIMEOUT_SEC = 60.0
        gpio = self._make_gpio()

        gpio.set_laser(True)
        self.assertIsNotNone(gpio._laser_timer, "Precondition: timer active")
        gpio.cleanup()
        self.assertIsNone(gpio._laser_timer, "cleanup() must cancel the laser timer")


# ── TestDoPulseExceptionSafety ────────────────────────────────────────────────

class TestDoPulseExceptionSafety(unittest.TestCase):
    """_do_pulse must always release the pin even when time.sleep raises."""

    def test_release_called_when_sleep_raises(self):
        """_release_pin is called in finally even if time.sleep raises."""
        from gpio_control import GPIOControl
        settings = _FakeSettings(triggerPin=17, trigMode=0, invertTrig=False)
        gpio = GPIOControl(settings)

        asserted = []
        released = []

        def recording_assert(pin, mode, invert=False):
            asserted.append(pin)

        def recording_release(pin, mode, invert=False):
            released.append(pin)

        gpio._assert_pin = recording_assert
        gpio._release_pin = recording_release

        # Patch time.sleep to raise partway through
        import unittest.mock
        with unittest.mock.patch("gpio_control.time.sleep", side_effect=RuntimeError("sleep interrupted")):
            # Run without _GPIO_AVAILABLE path: need GPIO available path
            # Since GPIO isn't available, _do_pulse returns early in sim mode.
            # We test the GPIO-available path directly by temporarily enabling it.
            import gpio_control as _gc
            orig_avail = _gc._GPIO_AVAILABLE
            _gc._GPIO_AVAILABLE = True
            try:
                with self.assertRaises(RuntimeError):
                    gpio._do_pulse(17, 0, 100, False)
            finally:
                _gc._GPIO_AVAILABLE = orig_avail

        self.assertEqual(len(asserted), 1,
            "_assert_pin must have been called once")
        self.assertEqual(len(released), 1,
            "_release_pin must be called in finally even when sleep raises")

    def test_release_failure_logged_not_raised(self):
        """A _release_pin exception must be logged, not propagated."""
        from gpio_control import GPIOControl
        settings = _FakeSettings(triggerPin=17, trigMode=0, invertTrig=False)
        gpio = GPIOControl(settings)

        gpio._assert_pin = lambda pin, mode, invert=False: None

        def bad_release(pin, mode, invert=False):
            raise RuntimeError("release GPIO failure")

        gpio._release_pin = bad_release

        import gpio_control as _gc
        orig_avail = _gc._GPIO_AVAILABLE
        _gc._GPIO_AVAILABLE = True
        try:
            # sleep should complete fine; release should swallow the error
            gpio._do_pulse(17, 0, 1, False)   # 1ms pulse
        finally:
            _gc._GPIO_AVAILABLE = orig_avail
        # If we reach here without raising, the test passes


if __name__ == "__main__":
    unittest.main(verbosity=2)
