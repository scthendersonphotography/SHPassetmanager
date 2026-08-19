"""
Astronomical event calculator for Pi TX.

Uses the `astral` library (v3.x) for solar and lunar computations.
Falls back gracefully when astral is not installed (returns all-None dicts).

Events and their meanings for photographers:
  SUNRISE           — exact moment the sun crosses the horizon (morning)
  SUNSET            — exact moment the sun crosses the horizon (evening)
  MOONRISE          — moon rises above the horizon
  MOONSET           — moon sets below the horizon
  GOLDEN_HOUR_START — start of the morning golden hour (warm low-angle light,
                      ~30–60 min before sunrise; astral golden_hour RISING [0])
  GOLDEN_HOUR_END   — end of the evening golden hour (warm low-angle light ends,
                      ~30–60 min after sunset; astral golden_hour SETTING [1])
  BLUE_HOUR_START   — start of the morning blue hour (deep pre-dawn twilight;
                      astral blue_hour RISING [0])
  BLUE_HOUR_END     — end of the evening blue hour (deep post-dusk twilight;
                      astral blue_hour SETTING [1])

All returned datetimes are UTC-aware.
Polar edge cases (sun never rises/sets, or moon not visible) return None for
the missing events; other events in the same call are still returned.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Optional

# ── Sentinels (allow test-patching even when astral is not installed) ─────────
ASTRAL_AVAILABLE  = False
LocationInfo       = None  # type: ignore[assignment]
sun                = None  # type: ignore[assignment]
golden_hour        = None  # type: ignore[assignment]
blue_hour          = None  # type: ignore[assignment]
SunDirection       = None  # type: ignore[assignment]
_moonrise          = None  # type: ignore[assignment]
_moonset           = None  # type: ignore[assignment]

try:
    from astral import LocationInfo, SunDirection         # noqa: F811
    from astral.sun import sun                            # noqa: F811
    from astral.sun import golden_hour                    # noqa: F811
    from astral.sun import blue_hour                      # noqa: F811
    from astral.moon import moonrise as _moonrise         # noqa: F811
    from astral.moon import moonset as _moonset           # noqa: F811
    ASTRAL_AVAILABLE = True
except ImportError:
    pass

# Canonical ordered list of all supported event names.
ASTRO_EVENTS = [
    "SUNRISE",
    "SUNSET",
    "MOONRISE",
    "MOONSET",
    "GOLDEN_HOUR_START",
    "GOLDEN_HOUR_END",
    "BLUE_HOUR_START",
    "BLUE_HOUR_END",
]


def compute_events(
    lat: float,
    lon: float,
    d: date,
) -> dict[str, Optional[datetime]]:
    """
    Compute all supported astronomical events for a given location and date.

    Returns a dict keyed by event name → UTC-aware datetime (or None).
    All values are None when astral is not installed or the location is
    within a polar region where the event does not occur on that date.
    """
    result: dict[str, Optional[datetime]] = {ev: None for ev in ASTRO_EVENTS}
    if not ASTRAL_AVAILABLE:
        return result

    try:
        obs = LocationInfo(latitude=lat, longitude=lon).observer

        # ── Solar rise/set ─────────────────────────────────────────────────────
        try:
            s = sun(obs, date=d, tzinfo=timezone.utc)
            result["SUNRISE"] = s.get("sunrise")
            result["SUNSET"]  = s.get("sunset")
        except Exception as exc:
            print(f"[astro] Solar rise/set failed for {d}: {exc}")

        # ── Golden hour (astral computes actual atmospheric refraction bounds) ─
        try:
            gh_morning = golden_hour(obs, date=d, direction=SunDirection.RISING)
            result["GOLDEN_HOUR_START"] = gh_morning[0]   # start of morning golden hour
        except Exception as exc:
            print(f"[astro] Golden hour (RISING) failed for {d}: {exc}")

        try:
            gh_evening = golden_hour(obs, date=d, direction=SunDirection.SETTING)
            result["GOLDEN_HOUR_END"] = gh_evening[1]    # end of evening golden hour
        except Exception as exc:
            print(f"[astro] Golden hour (SETTING) failed for {d}: {exc}")

        # ── Blue hour (twilight window adjacent to golden hour) ────────────────
        try:
            bh_morning = blue_hour(obs, date=d, direction=SunDirection.RISING)
            result["BLUE_HOUR_START"] = bh_morning[0]   # start of morning blue hour
        except Exception as exc:
            print(f"[astro] Blue hour (RISING) failed for {d}: {exc}")

        try:
            bh_evening = blue_hour(obs, date=d, direction=SunDirection.SETTING)
            result["BLUE_HOUR_END"] = bh_evening[1]     # end of evening blue hour
        except Exception as exc:
            print(f"[astro] Blue hour (SETTING) failed for {d}: {exc}")

        # ── Lunar events ───────────────────────────────────────────────────────
        try:
            result["MOONRISE"] = _moonrise(obs, date=d, tzinfo=timezone.utc)
        except Exception as exc:
            print(f"[astro] Moonrise failed for {d}: {exc}")

        try:
            result["MOONSET"] = _moonset(obs, date=d, tzinfo=timezone.utc)
        except Exception as exc:
            print(f"[astro] Moonset failed for {d}: {exc}")

    except Exception as exc:
        print(f"[astro] Event computation failed: {exc}")

    return result
