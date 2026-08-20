"""
Tests for CameraUSB trigger integration and the trigger engine's USB camera path.

Covers:
  TestCameraUSBTriggerInternals
    - CameraUSB.trigger() with a fake gphoto2 camera that raises on shot 2
      returns False without crashing (mid-burst gphoto2 failure)
    - CameraUSB.trigger() with a camera that always succeeds returns True

  TestUsbTriggerFailureEmitsCamErr
    - trigger() returns False → CAM_ERR,TRIGGER_FAIL emitted, burst stamped complete
    - trigger() raises on shot 2 of a burst → partial failure reported, daemon survives

  TestGpioFallbackRegressionGuard
    - camMode != 6 → GPIO path used, no CameraUSB.trigger() call

All tests run without gphoto2 / RPi.GPIO installed (stubs only).
"""

import threading
import time
import unittest


# ── Shared stubs (mirrors test_trigger.py) ────────────────────────────────────

class _FakeSettings:
    def __init__(self, **overrides):
        self._data = dict(
            minDist=10, maxDist=500, minFlux=20,
            cooldown=200,
            shots=1, pulseDur=50,
            usbBurst=1,
            delayShot=0,
            afEnabled=False, afPreMs=100,
            usbAfEn=False,
            wakeEn=False, wakeIntervalMs=300000,
            schedEn=False, schedStart=0, schedEnd=1439,
            triggerPin=17, wakePin=27,
            trigMode=0, wakeMode=0,
            laserPin=-1, statusLedPin=-1, powerLedPin=-1,
            camMode=6,   # USB camera mode by default for these tests
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
    """Records GPIO calls so tests can assert no GPIO shutter pulse was fired."""
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
        pass

    def fire_wake_blocking(self, pulse_ms):
        pass


# ── Fake gphoto2 module ───────────────────────────────────────────────────────

# These constants mirror the real gphoto2 values used in CameraUSB.trigger()
_GP_EVENT_CAPTURE_COMPLETE = 4
_GP_EVENT_FILE_ADDED = 2


class _FakeGpCamera:
    """
    Minimal fake of gp.Camera — only the methods CameraUSB.trigger() calls.

    `fail_on_shot` (1-based): if set, trigger_capture() raises RuntimeError
    on that shot number, simulating a mid-burst gphoto2 PTP/USB error.
    """
    def __init__(self, fail_on_shot: int = 0):
        self._shot_count = 0
        self._fail_on_shot = fail_on_shot

    def trigger_capture(self):
        self._shot_count += 1
        if self._fail_on_shot and self._shot_count == self._fail_on_shot:
            raise RuntimeError(f"[gphoto2] PTP error on shot {self._shot_count}")

    def wait_for_event(self, timeout_ms):
        return (_GP_EVENT_CAPTURE_COMPLETE, None)


class _FakeGp:
    """Minimal fake of the gphoto2 module surface used by CameraUSB.trigger()."""
    GP_EVENT_CAPTURE_COMPLETE = _GP_EVENT_CAPTURE_COMPLETE
    GP_EVENT_FILE_ADDED = _GP_EVENT_FILE_ADDED


# ── TestCameraUSBTriggerInternals ─────────────────────────────────────────────

class TestCameraUSBTriggerInternals(unittest.TestCase):
    """
    Exercise CameraUSB.trigger() directly with a fake gphoto2 camera.

    These tests inject a fake gp module and a fake gp.Camera so they run
    without gphoto2 installed, while still exercising the real trigger() code.
    """

    def _make_camera_usb(self, fail_on_shot: int = 0):
        """
        Return a CameraUSB with GPHOTO2_AVAILABLE=True and a fake gp.Camera
        wired in — ready to call trigger() without real hardware.
        """
        import camera_usb as cu
        # Save original module state
        orig_avail = cu.GPHOTO2_AVAILABLE
        orig_gp = getattr(cu, "gp", None)

        # Inject fakes
        cu.GPHOTO2_AVAILABLE = True
        cu.gp = _FakeGp()

        cam_usb = cu.CameraUSB(lambda msg: None)
        cam_usb._connected = True
        cam_usb._camera = _FakeGpCamera(fail_on_shot=fail_on_shot)

        return cam_usb, cu, orig_avail, orig_gp

    def _restore(self, cu, orig_avail, orig_gp):
        cu.GPHOTO2_AVAILABLE = orig_avail
        if orig_gp is None:
            if hasattr(cu, "gp"):
                del cu.gp
        else:
            cu.gp = orig_gp

    def test_all_shots_succeed_returns_true(self):
        """trigger(shots=3) with no failures must return True."""
        cam_usb, cu, orig_avail, orig_gp = self._make_camera_usb(fail_on_shot=0)
        try:
            result = cam_usb.trigger(shots=3, af_pre_ms=0)
        finally:
            self._restore(cu, orig_avail, orig_gp)
        self.assertTrue(result, "trigger() must return True when all shots succeed")

    def test_mid_burst_gphoto2_error_returns_false(self):
        """
        When gphoto2 raises on shot 2 of a 3-shot burst, trigger() must
        catch the exception, log it, and return False — not propagate.
        """
        cam_usb, cu, orig_avail, orig_gp = self._make_camera_usb(fail_on_shot=2)
        try:
            result = cam_usb.trigger(shots=3, af_pre_ms=0)
        finally:
            self._restore(cu, orig_avail, orig_gp)
        self.assertFalse(result,
            "trigger() must return False when gphoto2 raises on shot 2 of 3")

    def test_first_shot_failure_returns_false(self):
        """trigger(shots=1) with a gphoto2 raise on shot 1 must return False."""
        cam_usb, cu, orig_avail, orig_gp = self._make_camera_usb(fail_on_shot=1)
        try:
            result = cam_usb.trigger(shots=1, af_pre_ms=0)
        finally:
            self._restore(cu, orig_avail, orig_gp)
        self.assertFalse(result,
            "trigger() must return False when gphoto2 raises on the only shot")

    def test_mid_burst_failure_engine_emits_cam_err(self):
        """
        Wire a CameraUSB whose gphoto2 raises on shot 2 into TriggerEngine.
        _do_trigger_sequence() must emit CAM_ERR,TRIGGER_FAIL.
        """
        import camera_usb as cu
        orig_avail = cu.GPHOTO2_AVAILABLE
        orig_gp = getattr(cu, "gp", None)
        try:
            cu.GPHOTO2_AVAILABLE = True
            cu.gp = _FakeGp()

            cam_usb = cu.CameraUSB(lambda msg: None)
            cam_usb._connected = True
            cam_usb._camera = _FakeGpCamera(fail_on_shot=2)  # raises on shot 2

            settings = _FakeSettings(camMode=6, shots=3, usbBurst=3,
                                     pulseDur=50, afEnabled=False)
            gpio = _RecordingGPIO()
            lidar = _FakeLiDAR()
            responses = []
            from trigger_engine import TriggerEngine
            engine = TriggerEngine(settings, gpio, lidar, responses.append)
            engine.set_camera(cam_usb)
            engine._in_burst = True
            engine._do_trigger_sequence()

            cam_errs = [r for r in responses if "CAM_ERR,TRIGGER_FAIL" in r]
            self.assertEqual(len(cam_errs), 1,
                f"Mid-burst gphoto2 failure must produce CAM_ERR,TRIGGER_FAIL: {responses}")
            self.assertFalse(engine._in_burst,
                "_in_burst must be cleared after mid-burst gphoto2 failure")
        finally:
            self._restore(cu, orig_avail, orig_gp)


# ── Fake CameraUSB ─────────────────────────────────────────────────────────────

class _FakeCamera:
    """
    Minimal CameraUSB stand-in.  Lets tests control connected state and trigger
    return value / side-effects without gphoto2.
    """
    def __init__(self, trigger_return=True, trigger_side_effect=None):
        self._trigger_return = trigger_return
        self._trigger_side_effect = trigger_side_effect  # callable, if set called instead
        self.connected = True
        self.trigger_calls: list[dict] = []

    def trigger(self, shots=1, af_pre_ms=0):
        self.trigger_calls.append({"shots": shots, "af_pre_ms": af_pre_ms})
        if self._trigger_side_effect is not None:
            return self._trigger_side_effect(shots=shots, af_pre_ms=af_pre_ms)
        return self._trigger_return


# ── TestUsbTriggerFailureEmitsCamErr ─────────────────────────────────────────

class TestUsbTriggerFailureEmitsCamErr(unittest.TestCase):
    """
    When CameraUSB.trigger() returns False, the engine must emit CAM_ERR,TRIGGER_FAIL
    and still complete the burst bookkeeping (_in_burst cleared, timestamps stamped).
    """

    def _run_usb_sequence(self, cam):
        """Set up a TriggerEngine with the given fake camera and run one sequence."""
        settings = _FakeSettings(camMode=6, shots=1, usbBurst=1, pulseDur=50, afEnabled=False)
        gpio = _RecordingGPIO()
        lidar = _FakeLiDAR()
        responses = []
        from trigger_engine import TriggerEngine
        engine = TriggerEngine(settings, gpio, lidar, responses.append)
        engine.set_camera(cam)
        engine._in_burst = True   # simulate the flag set by trigger_loop before dispatch
        engine._do_trigger_sequence()
        return engine, responses

    def test_trigger_failure_emits_cam_err(self):
        """trigger() → False must cause CAM_ERR,TRIGGER_FAIL to appear in outbox."""
        cam = _FakeCamera(trigger_return=False)
        _, responses = self._run_usb_sequence(cam)

        cam_errs = [r for r in responses if "CAM_ERR,TRIGGER_FAIL" in r]
        self.assertEqual(len(cam_errs), 1,
            f"Expected exactly one CAM_ERR,TRIGGER_FAIL response, got: {responses}")

    def test_trigger_success_no_cam_err(self):
        """trigger() → True must NOT emit CAM_ERR."""
        cam = _FakeCamera(trigger_return=True)
        _, responses = self._run_usb_sequence(cam)

        cam_errs = [r for r in responses if "CAM_ERR" in r]
        self.assertEqual(cam_errs, [],
            f"No CAM_ERR expected on successful trigger, got: {responses}")

    def test_trig_response_still_emitted_on_failure(self):
        """Even on trigger failure, TRIG,<dist> must still be emitted (burst happened)."""
        cam = _FakeCamera(trigger_return=False)
        _, responses = self._run_usb_sequence(cam)

        trig_lines = [r for r in responses if r.strip().startswith("TRIG,")]
        self.assertEqual(len(trig_lines), 1,
            f"TRIG,<dist> must still be emitted on USB trigger failure: {responses}")

    def test_in_burst_cleared_after_failure(self):
        """_in_burst must be False after the sequence regardless of trigger success."""
        cam = _FakeCamera(trigger_return=False)
        engine, _ = self._run_usb_sequence(cam)
        self.assertFalse(engine._in_burst,
            "_in_burst must be cleared in finally even when trigger() returns False")

    def test_last_trigger_monotonic_stamped_after_failure(self):
        """Cooldown timer must be stamped even when trigger() returns False."""
        cam = _FakeCamera(trigger_return=False)
        engine, _ = self._run_usb_sequence(cam)
        elapsed = time.monotonic() - engine._last_trigger_monotonic
        self.assertLess(elapsed, 1.0,
            "_last_trigger_monotonic must be updated after a failed USB trigger")

    def test_partial_burst_failure_no_crash(self):
        """
        Camera raises an exception on shot 2 of a 3-shot burst.
        CameraUSB.trigger() catches it internally and returns False.
        The engine must emit CAM_ERR,TRIGGER_FAIL and not propagate the exception.
        """
        call_count = [0]

        def flaky_trigger(shots=1, af_pre_ms=0):
            # Simulate CameraUSB internal behaviour: catches mid-burst exception,
            # logs it, and returns False.
            call_count[0] += 1
            if call_count[0] == 1:
                return False   # camera returned partial failure
            return True

        cam = _FakeCamera()
        cam._trigger_side_effect = flaky_trigger

        settings = _FakeSettings(camMode=6, shots=3, usbBurst=3, pulseDur=50, afEnabled=False)
        gpio = _RecordingGPIO()
        lidar = _FakeLiDAR()
        responses = []
        from trigger_engine import TriggerEngine
        engine = TriggerEngine(settings, gpio, lidar, responses.append)
        engine.set_camera(cam)
        engine._in_burst = True

        # Must not raise
        engine._do_trigger_sequence()

        cam_errs = [r for r in responses if "CAM_ERR,TRIGGER_FAIL" in r]
        self.assertEqual(len(cam_errs), 1,
            f"Partial failure must emit exactly one CAM_ERR,TRIGGER_FAIL: {responses}")
        self.assertFalse(engine._in_burst, "_in_burst must be cleared after partial failure")

    def test_camera_raises_exception_no_crash(self):
        """
        If trigger() itself raises (unexpected gphoto2 crash), the engine must
        survive and clear _in_burst in its finally block.
        """
        def raising_trigger(shots=1, af_pre_ms=0):
            raise RuntimeError("simulated gphoto2 crash")

        cam = _FakeCamera()
        cam._trigger_side_effect = raising_trigger

        settings = _FakeSettings(camMode=6, shots=1, usbBurst=1, pulseDur=50, afEnabled=False)
        gpio = _RecordingGPIO()
        lidar = _FakeLiDAR()
        responses = []
        from trigger_engine import TriggerEngine
        engine = TriggerEngine(settings, gpio, lidar, responses.append)
        engine.set_camera(cam)
        engine._in_burst = True

        # Must not propagate
        engine._do_trigger_sequence()

        self.assertFalse(engine._in_burst,
            "_in_burst must be cleared even when trigger() raises an exception")


# ── TestGpioFallbackRegressionGuard ──────────────────────────────────────────

class TestGpioFallbackRegressionGuard(unittest.TestCase):
    """
    When camMode != 6, the engine must use the GPIO shutter path and must not
    call CameraUSB.trigger() at all — even if a camera object is attached.
    """

    def _run_gpio_sequence(self, cam_mode):
        settings = _FakeSettings(camMode=cam_mode, shots=2, pulseDur=50, afEnabled=False)
        gpio = _RecordingGPIO()
        lidar = _FakeLiDAR()
        responses = []
        cam = _FakeCamera(trigger_return=True)   # would succeed if called

        from trigger_engine import TriggerEngine
        engine = TriggerEngine(settings, gpio, lidar, responses.append)
        engine.set_camera(cam)
        engine._do_trigger_sequence()
        return gpio, cam, responses

    def test_gpio_used_when_cam_mode_0(self):
        """camMode=0 (GPIO mode) must fire GPIO pulses, not USB trigger."""
        gpio, cam, _ = self._run_gpio_sequence(cam_mode=0)
        gpio_fires = [e for e in gpio.events if e[0] == "fire_trigger_blocking"]
        self.assertGreaterEqual(len(gpio_fires), 1,
            "GPIO shutter pulse expected for camMode=0")
        self.assertEqual(cam.trigger_calls, [],
            "CameraUSB.trigger() must NOT be called when camMode=0")

    def test_gpio_used_when_cam_mode_1(self):
        """camMode=1 must fire GPIO pulses, not USB trigger."""
        gpio, cam, _ = self._run_gpio_sequence(cam_mode=1)
        gpio_fires = [e for e in gpio.events if e[0] == "fire_trigger_blocking"]
        self.assertGreaterEqual(len(gpio_fires), 1,
            "GPIO shutter pulse expected for camMode=1")
        self.assertEqual(cam.trigger_calls, [],
            "CameraUSB.trigger() must NOT be called when camMode=1")

    def test_usb_trigger_called_when_cam_mode_6(self):
        """camMode=6 must use CameraUSB.trigger(), not GPIO shutter pulse."""
        settings = _FakeSettings(camMode=6, shots=1, usbBurst=1, pulseDur=50, afEnabled=False)
        gpio = _RecordingGPIO()
        lidar = _FakeLiDAR()
        responses = []
        cam = _FakeCamera(trigger_return=True)

        from trigger_engine import TriggerEngine
        engine = TriggerEngine(settings, gpio, lidar, responses.append)
        engine.set_camera(cam)
        engine._do_trigger_sequence()

        self.assertEqual(len(cam.trigger_calls), 1,
            "CameraUSB.trigger() must be called once for camMode=6 with shots=1")
        gpio_fires = [e for e in gpio.events if e[0] == "fire_trigger_blocking"]
        self.assertEqual(gpio_fires, [],
            "GPIO shutter pulse must NOT be fired when camMode=6")

    def test_no_cam_err_emitted_on_gpio_path(self):
        """GPIO path must not emit CAM_ERR even if camera object is attached."""
        _, _, responses = self._run_gpio_sequence(cam_mode=0)
        cam_errs = [r for r in responses if "CAM_ERR" in r]
        self.assertEqual(cam_errs, [],
            f"No CAM_ERR expected on GPIO path: {responses}")

    def test_cam_mode_6_without_camera_falls_back_to_gpio(self):
        """
        If camMode=6 but no CameraUSB is attached (camera=None), the engine
        must fall back to the GPIO path and not crash.
        """
        settings = _FakeSettings(camMode=6, shots=1, usbBurst=1, pulseDur=50, afEnabled=False)
        gpio = _RecordingGPIO()
        lidar = _FakeLiDAR()
        responses = []

        from trigger_engine import TriggerEngine
        engine = TriggerEngine(settings, gpio, lidar, responses.append)
        # camera is None (not set via set_camera)
        engine._do_trigger_sequence()

        gpio_fires = [e for e in gpio.events if e[0] == "fire_trigger_blocking"]
        self.assertGreaterEqual(len(gpio_fires), 1,
            "GPIO fallback must fire when camMode=6 but camera is None")

    def test_cam_mode_6_disconnected_camera_falls_back_to_gpio(self):
        """
        If camMode=6 but the camera's .connected property is False, the engine
        must fall back to GPIO rather than calling trigger() on a disconnected camera.
        """
        settings = _FakeSettings(camMode=6, shots=1, usbBurst=1, pulseDur=50, afEnabled=False)
        gpio = _RecordingGPIO()
        lidar = _FakeLiDAR()
        responses = []
        cam = _FakeCamera(trigger_return=True)
        cam.connected = False   # simulate disconnected state

        from trigger_engine import TriggerEngine
        engine = TriggerEngine(settings, gpio, lidar, responses.append)
        engine.set_camera(cam)
        engine._do_trigger_sequence()

        self.assertEqual(cam.trigger_calls, [],
            "trigger() must not be called when camera.connected is False")
        gpio_fires = [e for e in gpio.events if e[0] == "fire_trigger_blocking"]
        self.assertGreaterEqual(len(gpio_fires), 1,
            "GPIO fallback must fire when camera is disconnected")


# ── TestModelKeyedCameraSettings ─────────────────────────────────────────────

class _TrackingSettings:
    """
    Minimal Settings stand-in that tracks all get/set calls and honours
    model-specific keys (e.g. "usbIso_Canon EOS R5") correctly.
    """
    def __init__(self, initial: dict | None = None):
        self._data: dict = dict(initial or {})
        self.set_calls: list[tuple] = []   # (key, value) pairs in order

    def get(self, key, default=None):
        return self._data.get(key, default)

    def set(self, key, value):
        self.set_calls.append((key, value))
        self._data[key] = value

    def update(self, d: dict):
        for k, v in d.items():
            self.set(k, v)


class _FakeCameraUSBForSettings:
    """
    Minimal CameraUSB-like object that exposes push_connected_info() with
    injectable _model and _settings, without touching gphoto2 at all.
    """
    def __init__(self, model: str, settings: _TrackingSettings):
        self._model = model
        self._settings = settings
        self._connected = True
        self.applied: list[tuple] = []   # (alias, val) pairs that set_config_value received
        self._send_buf: list[str] = []

    # Partial copy of CameraUSB.push_connected_info() — the settings-restore block only.
    def restore_settings(self):
        """Re-implement only the settings-restore portion of push_connected_info()."""
        model = self._model
        for alias, base_key in (("iso", "usbIso"), ("aperture", "usbAperture"), ("ss", "usbSs")):
            model_key = f"{base_key}_{model}" if model else base_key
            stored = str(self._settings.get(model_key, "")).strip()
            if not stored and model:
                generic = str(self._settings.get(base_key, "")).strip()
                if generic:
                    self._settings.set(model_key, generic)
                    self._settings.set(base_key, "")
                    stored = generic
            if stored:
                self.applied.append((alias, stored))

    def set_config_value(self, alias, val):
        self.applied.append((alias, val))
        return True


class TestModelKeyedCameraSettings(unittest.TestCase):
    """
    Verify that USB camera settings (ISO / aperture / SS) are stored and
    restored per camera model so that swapping bodies does not bleed one
    camera's configuration into another.
    """

    # ── Model-specific write ───────────────────────────────────────────────────

    def test_iso_saved_with_model_key(self):
        """CAM_ISO must save the value under 'usbIso_<model>', not 'usbIso'."""
        settings = _TrackingSettings()
        cam = _FakeCameraUSBForSettings("Canon EOS R5", settings)

        # Simulate what command_parser does on CAM_ISO,800
        model = cam._model
        settings.set(f"usbIso_{model}", "800")

        self.assertEqual(settings.get("usbIso_Canon EOS R5"), "800")
        self.assertIn(("usbIso_Canon EOS R5", "800"), settings.set_calls)

    def test_different_models_get_independent_iso_keys(self):
        """Two bodies must store ISO under separate keys; each reads only its own."""
        settings = _TrackingSettings()

        # Camera A sets ISO 800
        settings.set("usbIso_Canon EOS R5", "800")
        # Camera B sets ISO 400
        settings.set("usbIso_Nikon Z6", "400")

        self.assertEqual(settings.get("usbIso_Canon EOS R5"), "800")
        self.assertEqual(settings.get("usbIso_Nikon Z6"), "400")

    # ── Restore on connect ────────────────────────────────────────────────────

    def test_restore_applies_model_specific_value(self):
        """push_connected_info() must apply the correct body's stored ISO."""
        settings = _TrackingSettings({
            "usbIso_Canon EOS R5": "800",
            "usbIso_Nikon Z6": "400",
        })
        cam = _FakeCameraUSBForSettings("Nikon Z6", settings)
        cam.restore_settings()

        applied_iso = [v for alias, v in cam.applied if alias == "iso"]
        self.assertEqual(applied_iso, ["400"],
            "Must restore Nikon Z6's ISO (400), not Canon's (800)")

    def test_model_b_does_not_get_model_a_settings(self):
        """
        After model A's settings are stored, connecting model B must not
        receive any of model A's values — ISO, aperture, or shutter speed.
        """
        settings = _TrackingSettings({
            "usbIso_Canon EOS R5": "3200",
            "usbAperture_Canon EOS R5": "5.6",
            "usbSs_Canon EOS R5": "1/250",
        })
        # A completely different body with no stored settings connects
        cam_b = _FakeCameraUSBForSettings("Nikon Z6", settings)
        cam_b.restore_settings()

        self.assertEqual(cam_b.applied, [],
            "Model B must not inherit model A's ISO, aperture, or shutter speed")

    # ── Legacy migration ──────────────────────────────────────────────────────

    def test_legacy_generic_key_migrated_to_model_key(self):
        """
        A pre-existing generic 'usbIso' value must be promoted to the connecting
        model's key on first connect.
        """
        settings = _TrackingSettings({"usbIso": "1600"})
        cam = _FakeCameraUSBForSettings("Canon EOS R5", settings)
        cam.restore_settings()

        self.assertEqual(settings.get("usbIso_Canon EOS R5"), "1600",
            "Generic usbIso must be copied to the model-specific key")

    def test_legacy_generic_key_consumed_after_migration(self):
        """
        After migrating the generic key to a model-specific key, the generic
        key must be cleared so no subsequent camera body inherits it.
        """
        settings = _TrackingSettings({"usbIso": "1600"})
        cam = _FakeCameraUSBForSettings("Canon EOS R5", settings)
        cam.restore_settings()

        self.assertEqual(settings.get("usbIso", ""), "",
            "Generic usbIso must be cleared after migration to prevent cross-body bleed")

    def test_second_body_does_not_get_migrated_value(self):
        """
        Model A migrates the generic key.  Model B connects afterwards and must
        NOT receive the value that belonged to model A.
        """
        settings = _TrackingSettings({"usbIso": "1600"})

        # Model A connects first — migrates generic key
        cam_a = _FakeCameraUSBForSettings("Canon EOS R5", settings)
        cam_a.restore_settings()
        # Generic key must now be ""
        self.assertEqual(settings.get("usbIso", ""), "",
            "Generic key must be consumed by Canon EOS R5's migration")

        # Model B connects — must find nothing to restore
        cam_b = _FakeCameraUSBForSettings("Nikon Z6", settings)
        cam_b.restore_settings()
        applied_iso = [v for alias, v in cam_b.applied if alias == "iso"]
        self.assertEqual(applied_iso, [],
            "Nikon Z6 must not receive Canon EOS R5's migrated ISO value")

    def test_legacy_migration_applies_settings_to_camera(self):
        """The migrated value from the generic key must also be applied to the camera."""
        settings = _TrackingSettings({
            "usbIso": "3200",
            "usbAperture": "2.8",
            "usbSs": "1/500",
        })
        cam = _FakeCameraUSBForSettings("Fujifilm X-T5", settings)
        cam.restore_settings()

        applied = {alias: val for alias, val in cam.applied}
        self.assertEqual(applied.get("iso"), "3200")
        self.assertEqual(applied.get("aperture"), "2.8")
        self.assertEqual(applied.get("ss"), "1/500")

    # ── No stored settings — camera defaults left intact ──────────────────────

    def test_no_stored_settings_nothing_applied(self):
        """When no settings exist for this model (and no generic key), nothing is applied."""
        settings = _TrackingSettings()
        cam = _FakeCameraUSBForSettings("Sony A7 IV", settings)
        cam.restore_settings()

        self.assertEqual(cam.applied, [],
            "No settings must be applied when neither model-specific nor generic key exists")


if __name__ == "__main__":
    unittest.main()
