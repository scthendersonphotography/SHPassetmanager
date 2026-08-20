"""
Tests for astro.py and AstroScheduler
"""
from __future__ import annotations

import unittest
from datetime import date, datetime, timezone
from unittest.mock import MagicMock, patch


# ── astro.compute_events ──────────────────────────────────────────────────────

class TestComputeEventsWhenAstralUnavailable(unittest.TestCase):
    """When astral is not installed, compute_events returns all-None dicts."""

    def test_returns_all_none_without_astral(self):
        import astro
        with patch.object(astro, "ASTRAL_AVAILABLE", False):
            result = astro.compute_events(51.5, -0.1, date(2026, 6, 21))
        from astro import ASTRO_EVENTS
        self.assertEqual(set(result.keys()), set(ASTRO_EVENTS))
        for k, v in result.items():
            self.assertIsNone(v, f"Expected None for {k}")


class TestComputeEventsWithMockedAstral(unittest.TestCase):
    """When astral IS available, compute_events maps events to datetimes."""

    def _make_sun_result(self) -> dict:
        base = datetime(2026, 6, 21, 4, 0, tzinfo=timezone.utc)
        from datetime import timedelta
        return {
            "dawn":    base,
            "sunrise": base + timedelta(hours=1),
            "noon":    base + timedelta(hours=5),
            "sunset":  base + timedelta(hours=16),
            "dusk":    base + timedelta(hours=17),
        }

    def _make_golden_blue_mocks(self):
        """Return (golden_hour_mock, blue_hour_mock) with realistic tuple return values."""
        from datetime import timedelta
        base = datetime(2026, 6, 21, 4, 0, tzinfo=timezone.utc)
        # morning golden hour: (start, end) — starts before sunrise
        gh_rising  = (base + timedelta(minutes=30), base + timedelta(hours=1, minutes=30))
        # evening golden hour: (start, end) — ends after sunset
        gh_setting = (base + timedelta(hours=14, minutes=30), base + timedelta(hours=15, minutes=30))
        # morning blue hour: before golden hour
        bh_rising  = (base + timedelta(minutes=10), base + timedelta(minutes=30))
        # evening blue hour: after golden hour
        bh_setting = (base + timedelta(hours=15, minutes=30), base + timedelta(hours=15, minutes=50))

        def _golden_hour_side_effect(obs, date, direction):
            from astral import SunDirection as SD
            return gh_rising if direction == SD.RISING else gh_setting

        def _blue_hour_side_effect(obs, date, direction):
            from astral import SunDirection as SD
            return bh_rising if direction == SD.RISING else bh_setting

        return _golden_hour_side_effect, _blue_hour_side_effect, gh_rising, gh_setting, bh_rising, bh_setting

    def test_solar_events_populated(self):
        import astro
        sun_result = self._make_sun_result()
        obs = MagicMock()
        loc_info = MagicMock()
        loc_info.observer = obs
        gh_se, bh_se, *_ = self._make_golden_blue_mocks()

        with patch.object(astro, "ASTRAL_AVAILABLE", True), \
             patch.object(astro, "LocationInfo", return_value=loc_info), \
             patch.object(astro, "sun", return_value=sun_result), \
             patch.object(astro, "golden_hour", side_effect=gh_se), \
             patch.object(astro, "blue_hour",   side_effect=bh_se), \
             patch.object(astro, "_moonrise", return_value=None), \
             patch.object(astro, "_moonset", return_value=None):
            result = astro.compute_events(51.5, -0.1, date(2026, 6, 21))

        self.assertIsNotNone(result["SUNRISE"])
        self.assertIsNotNone(result["SUNSET"])
        self.assertIsNotNone(result["GOLDEN_HOUR_START"])
        self.assertIsNotNone(result["GOLDEN_HOUR_END"])
        self.assertIsNotNone(result["BLUE_HOUR_START"])
        self.assertIsNotNone(result["BLUE_HOUR_END"])

    def test_golden_hour_uses_astral_golden_hour_api(self):
        """GOLDEN_HOUR_START/END come from astral golden_hour(), not sunrise/sunset aliases."""
        import astro
        sun_result = self._make_sun_result()
        obs = MagicMock()
        loc_info = MagicMock()
        loc_info.observer = obs
        gh_se, bh_se, gh_rising, gh_setting, bh_rising, bh_setting = self._make_golden_blue_mocks()

        with patch.object(astro, "ASTRAL_AVAILABLE", True), \
             patch.object(astro, "LocationInfo", return_value=loc_info), \
             patch.object(astro, "sun", return_value=sun_result), \
             patch.object(astro, "golden_hour", side_effect=gh_se), \
             patch.object(astro, "blue_hour",   side_effect=bh_se), \
             patch.object(astro, "_moonrise", return_value=None), \
             patch.object(astro, "_moonset", return_value=None):
            result = astro.compute_events(51.5, -0.1, date(2026, 6, 21))

        # Golden hour boundaries come from astral golden_hour(), not sunrise/sunset
        self.assertEqual(result["GOLDEN_HOUR_START"], gh_rising[0])   # start of morning golden hour
        self.assertEqual(result["GOLDEN_HOUR_END"],   gh_setting[1])  # end of evening golden hour
        # They must NOT equal sunrise/sunset (which are later/earlier respectively)
        self.assertNotEqual(result["GOLDEN_HOUR_START"], result["SUNRISE"])
        self.assertNotEqual(result["GOLDEN_HOUR_END"],   result["SUNSET"])
        # Blue hour boundaries
        self.assertEqual(result["BLUE_HOUR_START"], bh_rising[0])
        self.assertEqual(result["BLUE_HOUR_END"],   bh_setting[1])

    def test_polar_solar_exception_returns_none(self):
        import astro
        obs = MagicMock()
        loc_info = MagicMock()
        loc_info.observer = obs

        with patch.object(astro, "ASTRAL_AVAILABLE", True), \
             patch.object(astro, "LocationInfo", return_value=loc_info), \
             patch.object(astro, "sun", side_effect=Exception("polar")), \
             patch.object(astro, "_moonrise", return_value=None), \
             patch.object(astro, "_moonset", return_value=None):
            result = astro.compute_events(89.0, 0.0, date(2026, 6, 21))

        self.assertIsNone(result["SUNRISE"])
        self.assertIsNone(result["SUNSET"])
        # Moon events independently attempted — also None here since mock returns None
        self.assertIsNone(result["MOONRISE"])
        self.assertIsNone(result["MOONSET"])

    def test_moonrise_populated_independently(self):
        import astro
        from datetime import timedelta
        moon_dt = datetime(2026, 6, 21, 20, 30, tzinfo=timezone.utc)
        sun_result = self._make_sun_result()
        obs = MagicMock()
        loc_info = MagicMock()
        loc_info.observer = obs

        with patch.object(astro, "ASTRAL_AVAILABLE", True), \
             patch.object(astro, "LocationInfo", return_value=loc_info), \
             patch.object(astro, "sun", return_value=sun_result), \
             patch.object(astro, "_moonrise", return_value=moon_dt), \
             patch.object(astro, "_moonset", return_value=None):
            result = astro.compute_events(51.5, -0.1, date(2026, 6, 21))

        self.assertEqual(result["MOONRISE"], moon_dt)
        self.assertIsNone(result["MOONSET"])


# ── AstroScheduler ────────────────────────────────────────────────────────────

def _make_scheduler(sent: list[str], lat=51.5, lon=-0.1):
    """Create an AstroScheduler with persistence disabled so tests are isolated."""
    from astro_scheduler import AstroScheduler
    from timelapse import TimeLapse

    tl = MagicMock(spec=TimeLapse)
    tl.running = False
    tl.start = MagicMock(return_value=True)

    # Suppress _load so the constructor doesn't read the real ~/.pi-tx file,
    # and suppress _save so test operations don't write to it.
    with patch.object(AstroScheduler, "_load", lambda self: None), \
         patch.object(AstroScheduler, "_save", lambda self: None):
        sched = AstroScheduler(timelapse=tl, send_response=sent.append, lat=lat, lon=lon)
    # Keep save suppressed for the lifetime of this scheduler instance.
    sched._save = lambda: None
    return sched, tl


class TestAstroSchedulerLocation(unittest.TestCase):
    """set_location updates lat/lon and triggers reschedule."""

    def test_set_location_updates_coords(self):
        sent = []
        sched, _ = _make_scheduler(sent, lat=None, lon=None)
        sched.set_location(40.7128, -74.0060)
        self.assertAlmostEqual(sched._lat, 40.7128)
        self.assertAlmostEqual(sched._lon, -74.0060)


class TestAstroSchedulerAddClear(unittest.TestCase):
    """add_schedule / clear_schedules work correctly."""

    def test_add_schedule_returns_id(self):
        sent = []
        sched, _ = _make_scheduler(sent)
        # Patch _arm so no real timer is armed
        sched._arm = MagicMock()
        sid = sched.add_schedule("SUNRISE", offset_min=0, duration_sec=3600, interval_sec=30)
        self.assertEqual(sid, "1")
        self.assertEqual(len(sched.get_schedules()), 1)

    def test_add_unknown_event_raises(self):
        sent = []
        sched, _ = _make_scheduler(sent)
        sched._arm = MagicMock()
        with self.assertRaises(ValueError):
            sched.add_schedule("RAIN", offset_min=0, duration_sec=60, interval_sec=5)

    def test_clear_removes_all(self):
        sent = []
        sched, _ = _make_scheduler(sent)
        sched._arm = MagicMock()
        sched.add_schedule("SUNRISE", 0, 3600, 30)
        sched.add_schedule("SUNSET",  0, 3600, 30)
        self.assertEqual(len(sched.get_schedules()), 2)
        sched.clear_schedules()
        self.assertEqual(len(sched.get_schedules()), 0)


class TestAstroSchedulerPreview(unittest.TestCase):
    """get_preview_lines returns ASTRO_TIMES lines when astral is available."""

    def test_no_location_returns_empty(self):
        sent = []
        sched, _ = _make_scheduler(sent, lat=None, lon=None)
        lines = sched.get_preview_lines()
        self.assertEqual(lines, [])

    def test_preview_lines_format(self):
        import astro
        from datetime import timedelta

        sent = []
        sched, _ = _make_scheduler(sent)
        base = datetime(2026, 8, 16, 6, 0, tzinfo=timezone.utc)
        fake_events = {
            "SUNRISE":           base,
            "SUNSET":            base + timedelta(hours=14),
            "MOONRISE":          base + timedelta(hours=18),
            "MOONSET":           base + timedelta(hours=28),
            "GOLDEN_HOUR_START": base,
            "GOLDEN_HOUR_END":   base + timedelta(hours=14),
            "BLUE_HOUR_START":   base - timedelta(minutes=30),
            "BLUE_HOUR_END":     base + timedelta(hours=14, minutes=30),
        }

        with patch("astro_scheduler.compute_events", return_value=fake_events):
            lines = sched.get_preview_lines(days=1)

        self.assertGreater(len(lines), 0)
        for line in lines:
            self.assertTrue(line.startswith("ASTRO_TIMES,"), line)
            parts = line.strip().split(",")
            self.assertEqual(len(parts), 4, line)


if __name__ == "__main__":
    unittest.main()
