"""
Astronomical event scheduler for Pi TX.

For each configured schedule entry the scheduler:
  1. Computes the next UTC occurrence of the named event (with optional offset).
  2. Arms a ``threading.Timer`` to fire the time-lapse engine at that moment.
  3. Automatically re-arms for the next day once the timer fires.

Schedules survive daemon restarts — they are persisted to
``~/.pi-tx/astro_schedules.json``.

Thread safety
─────────────
``_lock`` guards ``_schedules``, ``_timers``, and ``_next_id``.
``_arm()`` is called with the lock released (it spawns a Timer).
"""

from __future__ import annotations

import json
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from timelapse import TimeLapse

# Import at module level so tests can patch astro_scheduler.compute_events
from astro import compute_events as compute_events  # noqa: E402

SCHEDULE_FILE = Path.home() / ".pi-tx" / "astro_schedules.json"


class AstroScheduler:
    def __init__(
        self,
        timelapse: "TimeLapse",
        send_response: Callable[[str], None],
        lat: Optional[float] = None,
        lon: Optional[float] = None,
    ) -> None:
        self._tl    = timelapse
        self._send  = send_response
        self._lat   = lat
        self._lon   = lon
        self._lock  = threading.Lock()
        self._schedules: list[dict] = []          # persisted entries
        self._timers:    dict[str, threading.Timer] = {}  # id → next Timer
        self._next_id = 1
        self._load()

    # ── Location ──────────────────────────────────────────────────────────────

    def set_location(self, lat: float, lon: float) -> None:
        """Update the observer location and reschedule all active entries."""
        self._lat = lat
        self._lon = lon
        self._reschedule_all()

    # ── Schedule CRUD ─────────────────────────────────────────────────────────

    def add_schedule(
        self,
        event: str,
        offset_min: int,
        duration_sec: int,
        interval_sec: int,
    ) -> str:
        """
        Add a new entry; return its string ID.
        Raises ValueError if event is not recognised.
        """
        from astro import ASTRO_EVENTS
        if event not in ASTRO_EVENTS:
            raise ValueError(f"Unknown event: {event!r}")

        with self._lock:
            sched_id = str(self._next_id)
            self._next_id += 1
            entry: dict = {
                "id":          sched_id,
                "event":       event,
                "offsetMin":   offset_min,
                "durationSec": duration_sec,
                "intervalSec": interval_sec,
            }
            self._schedules.append(entry)

        self._save()
        self._arm(entry)
        return sched_id

    def remove_schedule(self, sched_id: str) -> bool:
        """
        Cancel the timer for *sched_id* and remove the entry.
        Returns True when found and removed, False when not found.
        """
        with self._lock:
            timer = self._timers.pop(sched_id, None)
            if timer:
                timer.cancel()
            before = len(self._schedules)
            self._schedules = [e for e in self._schedules if e["id"] != sched_id]
            removed = len(self._schedules) < before
        if removed:
            self._save()
        return removed

    def clear_schedules(self) -> None:
        """Cancel all active timers and remove all entries."""
        with self._lock:
            for timer in self._timers.values():
                timer.cancel()
            self._timers.clear()
            self._schedules.clear()
            self._next_id = 1
        self._save()

    def get_schedules(self) -> list[dict]:
        """Return a snapshot of the current schedule list."""
        with self._lock:
            return list(self._schedules)

    # ── Preview ───────────────────────────────────────────────────────────────

    def get_preview_lines(self, days: int = 2) -> list[str]:
        """
        Return ``ASTRO_TIMES,<event>,<HH:MM>,<YYYY-MM-DD>`` lines for
        ``days`` consecutive days starting today (local time), showing each
        event time in the device's local timezone.

        Returns an empty list when the location is unset or astral is absent.
        """
        if self._lat is None or self._lon is None:
            return []

        lines: list[str] = []
        today_local = datetime.now().date()   # local date for display

        for delta in range(days):
            d = today_local + timedelta(days=delta)
            events = compute_events(self._lat, self._lon, d)
            for event_name, dt in events.items():
                if dt is None:
                    continue
                local_dt = dt.astimezone()     # convert UTC → device local tz
                hhmm     = local_dt.strftime("%H:%M")
                lines.append(f"ASTRO_TIMES,{event_name},{hhmm},{d.isoformat()}\n")

        return lines

    # ── Internal scheduling ───────────────────────────────────────────────────

    def _arm(self, entry: dict) -> None:
        """
        Compute the next UTC firing time for *entry* (today or tomorrow),
        cancel any existing timer for this entry's id, and arm a new one.
        Does nothing when the location is unset or astral is unavailable.
        """
        if self._lat is None or self._lon is None:
            return

        sched_id   = entry["id"]
        now_utc    = datetime.now(timezone.utc)
        fire_at: Optional[datetime] = None

        # Try today, then tomorrow.
        for delta in range(3):
            check_date = (now_utc + timedelta(days=delta)).date()
            events = compute_events(self._lat, self._lon, check_date)
            base = events.get(entry["event"])
            if base is None:
                continue
            candidate = base + timedelta(minutes=entry["offsetMin"])
            if candidate > now_utc + timedelta(seconds=5):  # 5 s buffer
                fire_at = candidate
                break

        if fire_at is None:
            print(f"[AstroSched] No upcoming {entry['event']} within 3 days (polar?)")
            return

        delay_sec = max(0.0, (fire_at - now_utc).total_seconds())
        local_str = fire_at.astimezone().strftime("%Y-%m-%d %H:%M %Z")
        print(f"[AstroSched] {entry['event']} fires in {delay_sec:.0f}s ({local_str})")

        def _fire(e: dict = entry) -> None:
            evt = e["event"]
            self._send(f"ASTRO_FIRED,{evt}\n")
            started = self._tl.start(e["intervalSec"], e["durationSec"])
            if not started:
                self._send("ASTRO_ERR,TIMELAPSE_BUSY\n")
                print(f"[AstroSched] {evt}: timelapse already running — skipped")
            # Re-arm for the next occurrence automatically.
            self._arm(e)

        with self._lock:
            old = self._timers.pop(sched_id, None)
            if old:
                old.cancel()
            timer = threading.Timer(delay_sec, _fire)
            timer.daemon = True
            timer.start()
            self._timers[sched_id] = timer

    def _reschedule_all(self) -> None:
        """Cancel all pending timers and re-arm from the current location."""
        with self._lock:
            for timer in self._timers.values():
                timer.cancel()
            self._timers.clear()
            schedules = list(self._schedules)

        for entry in schedules:
            self._arm(entry)

    # ── Persistence ───────────────────────────────────────────────────────────

    def _save(self) -> None:
        try:
            SCHEDULE_FILE.parent.mkdir(parents=True, exist_ok=True)
            with self._lock:
                payload = {
                    "schedules": list(self._schedules),
                    "next_id":   self._next_id,
                }
            with open(SCHEDULE_FILE, "w") as f:
                json.dump(payload, f, indent=2)
        except Exception as exc:
            print(f"[AstroSched] Save failed: {exc}")

    def _load(self) -> None:
        try:
            if not SCHEDULE_FILE.exists():
                return
            with open(SCHEDULE_FILE) as f:
                data = json.load(f)
            self._schedules = data.get("schedules", [])
            self._next_id   = data.get("next_id", len(self._schedules) + 1)
            print(f"[AstroSched] Loaded {len(self._schedules)} schedule(s)")
            for entry in self._schedules:
                self._arm(entry)
        except Exception as exc:
            print(f"[AstroSched] Load failed: {exc}")
