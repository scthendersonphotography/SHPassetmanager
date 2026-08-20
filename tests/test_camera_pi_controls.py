"""
Unit tests for CameraPi's Pi Camera 3 control semantics using a mock
picamera2 handle that enforces libcamera control ranges.

Covers all four shutter/gain auto/manual combinations, AF single-flight,
resolution reconfigure control reapplication, and parser-side validation.
Runs without picamera2/real hardware.
"""

from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import camera_pi
from camera_pi import CameraPi, AF_MODES, AWB_PRESETS


class FakeSettings:
    def __init__(self):
        self._d = {}

    def get(self, key, default=None):
        return self._d.get(key, default)

    def set(self, key, value):
        self._d[key] = value


class FakePicam:
    """Mimics Picamera2's control interface with range enforcement."""

    def __init__(self, model="imx708", exposure_us=20000, gain=2.5):
        self.camera_properties = {"Model": model}
        self.applied = []          # history of set_controls dicts
        self.started = False
        self._meta = {"ExposureTime": exposure_us, "AnalogueGain": gain}
        self.af_result = True

    def set_controls(self, controls):
        # Enforce libcamera-style valid ranges: zero sentinels are invalid.
        if "ExposureTime" in controls and controls["ExposureTime"] < 100:
            raise RuntimeError(f"ExposureTime out of range: {controls['ExposureTime']}")
        if "AnalogueGain" in controls and controls["AnalogueGain"] < 1.0:
            raise RuntimeError(f"AnalogueGain out of range: {controls['AnalogueGain']}")
        if "AfMode" in controls and controls["AfMode"] not in (0, 1, 2):
            raise RuntimeError("bad AfMode")
        if "AwbMode" in controls and controls["AwbMode"] not in AWB_PRESETS.values():
            raise RuntimeError("bad AwbMode")
        self.applied.append(dict(controls))

    def capture_metadata(self):
        return dict(self._meta)

    def autofocus_cycle(self):
        time.sleep(0.05)
        return self.af_result

    def start(self):
        self.started = True

    def stop(self):
        self.started = False

    def close(self):
        pass

    def capture_file(self, path):
        Path(path).write_bytes(b"\xff\xd8jpeg\xff\xd9")

    def configure(self, config):
        pass

    def create_still_configuration(self, main=None):
        return {"main": main}


def make_camera(fake=None, settings=None):
    """Wire a CameraPi directly to a FakePicam (bypasses connect probing)."""
    sent = []
    settings = settings if settings is not None else FakeSettings()
    cam = CameraPi(lambda line: sent.append(line.strip()), settings=settings)
    fake = fake or FakePicam()
    cam._picam = fake
    cam._connected = True
    cam._model = fake.camera_properties["Model"]
    cam._has_af = "imx708" in cam._model.lower()
    return cam, fake, sent, settings


class TestExposureGainCombos(unittest.TestCase):
    def test_both_auto_omits_manual_controls(self):
        cam, fake, _, _ = make_camera()
        self.assertTrue(cam._apply_controls())
        last = fake.applied[-1]
        self.assertTrue(last["AeEnable"])
        self.assertNotIn("ExposureTime", last)
        self.assertNotIn("AnalogueGain", last)

    def test_both_manual(self):
        cam, fake, _, _ = make_camera()
        self.assertTrue(cam.set_exposure("250"))
        self.assertTrue(cam.set_gain("4"))
        last = fake.applied[-1]
        self.assertFalse(last["AeEnable"])
        self.assertEqual(last["ExposureTime"], 250_000)
        self.assertEqual(last["AnalogueGain"], 4.0)

    def test_manual_shutter_freezes_gain_from_metadata(self):
        cam, fake, _, settings = make_camera(FakePicam(gain=3.7))
        self.assertTrue(cam.set_exposure("50"))
        last = fake.applied[-1]
        self.assertFalse(last["AeEnable"])
        self.assertEqual(last["ExposureTime"], 50_000)
        self.assertAlmostEqual(last["AnalogueGain"], 3.7)
        # Frozen value is persisted and reported — not a fictitious "auto"
        self.assertEqual(cam.gain, "3.7")

    def test_manual_gain_freezes_shutter_from_metadata(self):
        cam, fake, _, _ = make_camera(FakePicam(exposure_us=8000))
        self.assertTrue(cam.set_gain("8"))
        last = fake.applied[-1]
        self.assertFalse(last["AeEnable"])
        self.assertEqual(last["AnalogueGain"], 8.0)
        self.assertEqual(last["ExposureTime"], 8_000)   # 8 ms frozen
        self.assertEqual(cam.exposure, "8")

    def test_back_to_auto(self):
        cam, fake, _, _ = make_camera()
        cam.set_exposure("100")
        cam.set_exposure("auto")
        cam.set_gain("auto")
        last = fake.applied[-1]
        self.assertTrue(last["AeEnable"])
        self.assertNotIn("ExposureTime", last)

    def test_invalid_values_rejected(self):
        cam, _, _, _ = make_camera()
        self.assertFalse(cam.set_exposure("banana"))
        self.assertFalse(cam.set_gain(""))
        # Clamping keeps values inside valid ranges
        self.assertTrue(cam.set_exposure("999999"))
        self.assertEqual(cam.exposure, "10000")
        self.assertTrue(cam.set_gain("0.1"))
        self.assertEqual(cam.gain, "1")

    def test_no_zero_sentinels_ever_sent(self):
        cam, fake, _, _ = make_camera()
        cam._apply_controls()
        cam.set_exposure("10")
        cam.set_gain("2")
        cam.set_exposure("auto")   # gain still manual → exposure frozen
        for ctrl in fake.applied:
            self.assertNotEqual(ctrl.get("ExposureTime"), 0)
            self.assertNotEqual(ctrl.get("AnalogueGain"), 0.0)


class TestAfAndAwb(unittest.TestCase):
    def test_af_modes(self):
        cam, fake, _, _ = make_camera()
        for mode in ("manual", "auto", "continuous"):
            self.assertTrue(cam.set_af_mode(mode))
            self.assertEqual(fake.applied[-1]["AfMode"], AF_MODES[mode])
        self.assertFalse(cam.set_af_mode("bogus"))

    def test_manual_mode_sets_lens_position(self):
        cam, fake, _, _ = make_camera()
        cam.set_af_mode("manual")
        cam.set_lens_position(2.5)
        last = fake.applied[-1]
        self.assertEqual(last["LensPosition"], 2.5)
        # Clamped
        cam.set_lens_position(99)
        self.assertEqual(cam.lens_position, 10.0)

    def test_awb_presets(self):
        cam, fake, _, _ = make_camera()
        self.assertTrue(cam.set_awb("Daylight"))
        last = fake.applied[-1]
        self.assertFalse(last["AwbEnable"])
        self.assertEqual(last["AwbMode"], AWB_PRESETS["Daylight"])
        self.assertTrue(cam.set_awb("auto"))
        self.assertTrue(fake.applied[-1]["AwbEnable"])
        self.assertFalse(cam.set_awb("Shade"))

    def test_af_trigger_single_flight(self):
        cam, fake, sent, _ = make_camera()
        # Fire many concurrent triggers; only one AF cycle may run
        threads = [threading.Thread(target=cam.af_trigger) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        time.sleep(0.3)
        results = [l for l in sent if l in ("CAM_PI_AF_LOCKED", "CAM_PI_AF_FAILED")]
        self.assertEqual(results, ["CAM_PI_AF_LOCKED"])

    def test_af_trigger_disconnected(self):
        cam, _, sent, _ = make_camera()
        cam._connected = False
        cam.af_trigger()
        self.assertIn("CAM_PI_AF_FAILED", sent)

    def test_non_camera3_noops(self):
        cam, fake, _, _ = make_camera(FakePicam(model="imx219"))
        self.assertFalse(cam.has_af)
        before = len(fake.applied)
        cam.set_af_mode("auto")
        cam.set_exposure("100")
        # Settings persist, but no controls pushed to non-AF hardware
        self.assertEqual(len(fake.applied), before)


class TestResolutionReapply(unittest.TestCase):
    def test_reconfigure_reapplies_controls(self):
        cam, fake, _, _ = make_camera()
        cam.set_exposure("100")
        cam.set_gain("4")
        fake.applied.clear()
        self.assertTrue(cam.set_resolution("4K"))
        # Controls must be pushed again after the pipeline restart
        last = fake.applied[-1]
        self.assertFalse(last["AeEnable"])
        self.assertEqual(last["ExposureTime"], 100_000)
        self.assertEqual(last["AnalogueGain"], 4.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
