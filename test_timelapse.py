"""
Tests for timelapse.py
"""
from __future__ import annotations

import threading
import time
import unittest
from unittest.mock import MagicMock, patch


def _make_timelapse(sent: list[str]) -> "TimeLapse":
    from timelapse import TimeLapse
    te = MagicMock()
    te.fire_manual = MagicMock()
    return TimeLapse(trigger_engine=te, send_response=sent.append)


class TestTimeLapseStart(unittest.TestCase):
    """start() returns True on the first call; False when already running."""

    def test_start_idle_returns_true(self):
        sent = []
        tl = _make_timelapse(sent)
        ok = tl.start(interval_sec=60, duration_sec=120)
        self.assertTrue(ok)
        tl.stop()

    def test_start_while_running_returns_false(self):
        sent = []
        tl = _make_timelapse(sent)
        tl.start(interval_sec=60, duration_sec=300)
        ok = tl.start(interval_sec=10, duration_sec=60)
        self.assertFalse(ok)
        tl.stop()


class TestTimeLapseStop(unittest.TestCase):
    """stop() halts the session and emits TIMELAPSE_DONE."""

    def test_stop_emits_done(self):
        sent = []
        tl = _make_timelapse(sent)
        tl.start(interval_sec=5, duration_sec=600)
        time.sleep(0.1)
        tl.stop()
        # Wait for thread to finish
        if tl._thread:
            tl._thread.join(timeout=2.0)
        self.assertFalse(tl.running)
        self.assertIn("TIMELAPSE_DONE\n", sent)

    def test_stop_without_start_is_harmless(self):
        sent = []
        tl = _make_timelapse(sent)
        tl.stop()   # should not raise
        self.assertFalse(tl.running)


class TestTimeLapseFiresShots(unittest.TestCase):
    """The engine fires fire_manual() at the configured interval."""

    def test_shots_fired_and_reported(self):
        sent = []
        tl = _make_timelapse(sent)
        # Very short interval so test is fast
        tl.start(interval_sec=1, duration_sec=3)
        # Wait for 1 shot
        deadline = time.monotonic() + 3.0
        while not any("TIMELAPSE_SHOT," in m for m in sent) and time.monotonic() < deadline:
            time.sleep(0.05)
        tl.stop()
        if tl._thread:
            tl._thread.join(timeout=2.0)
        shot_msgs = [m for m in sent if m.startswith("TIMELAPSE_SHOT,")]
        self.assertGreater(len(shot_msgs), 0, "At least one TIMELAPSE_SHOT must be emitted")
        # First message should be TIMELAPSE_SHOT,1
        self.assertEqual(shot_msgs[0], "TIMELAPSE_SHOT,1\n")

    def test_done_emitted_after_duration_expires(self):
        sent = []
        tl = _make_timelapse(sent)
        tl.start(interval_sec=10, duration_sec=1)  # duration < interval → fires once then done
        if tl._thread:
            tl._thread.join(timeout=5.0)
        self.assertFalse(tl.running)
        self.assertIn("TIMELAPSE_DONE\n", sent)


class TestTimeLapseStatus(unittest.TestCase):
    """status_line() returns the correct format."""

    def test_idle_status(self):
        sent = []
        tl = _make_timelapse(sent)
        line = tl.status_line()
        self.assertEqual(line, "TIMELAPSE,idle,0,0\n")

    def test_running_status_format(self):
        sent = []
        tl = _make_timelapse(sent)
        tl.start(interval_sec=5, duration_sec=300)
        line = tl.status_line()
        self.assertTrue(line.startswith("TIMELAPSE,running,"), line)
        tl.stop()


class TestTimeLapseRestart(unittest.TestCase):
    """After a session completes, a new one can be started."""

    def test_restart_after_done(self):
        sent = []
        tl = _make_timelapse(sent)
        tl.start(interval_sec=10, duration_sec=1)
        if tl._thread:
            tl._thread.join(timeout=5.0)
        self.assertFalse(tl.running)
        ok = tl.start(interval_sec=10, duration_sec=1)
        self.assertTrue(ok, "Should be able to restart after session completes")
        tl.stop()


class TestTimeLapseDrift(unittest.TestCase):
    """Intervals stay within ±50 ms even when the trigger takes measurable time."""

    def test_intervals_within_50ms_under_simulated_load(self):
        """
        fire_manual() takes ~30 ms each call (simulated CPU load).  With the
        old ``next_shot = now + interval`` implementation that drift would
        accumulate; the anchored scheduler must keep every gap within ±50 ms.
        """
        from timelapse import TimeLapse

        shot_times: list[float] = []

        te = MagicMock()

        def slow_fire() -> bool:
            time.sleep(0.030)  # simulate 30 ms trigger overhead
            shot_times.append(time.monotonic())
            return True

        te.fire_manual = slow_fire

        sent: list[str] = []
        tl = TimeLapse(trigger_engine=te, send_response=sent.append)

        interval = 1  # 1-second interval (minimum accepted by start())
        duration = 4  # gives ~4 shots
        tl.start(interval_sec=interval, duration_sec=duration)
        if tl._thread:
            tl._thread.join(timeout=10.0)

        self.assertGreaterEqual(
            len(shot_times), 3,
            "Need at least 3 shots to verify drift behaviour",
        )

        for i in range(1, len(shot_times)):
            gap = shot_times[i] - shot_times[i - 1]
            drift_ms = abs(gap - interval) * 1000
            self.assertLessEqual(
                drift_ms,
                50.0,
                f"Shot {i + 1}: inter-shot gap {gap * 1000:.1f} ms drifted "
                f"{drift_ms:.1f} ms from the {interval * 1000:.0f} ms interval "
                f"(limit: ±50 ms)",
            )


if __name__ == "__main__":
    unittest.main()
