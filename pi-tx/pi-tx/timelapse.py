"""
Time-lapse engine for Pi TX.

Runs a background thread that fires the trigger every ``interval_sec`` seconds
for a total of ``duration_sec`` seconds.  Each shot routes through
``TriggerEngine.fire_manual()`` so GPIO pulse / USB camera shutter, burst
count, AF pre-pulse, and delay-before-shot settings all apply automatically.

Thread safety
─────────────
``start()`` / ``stop()`` may be called from any thread (HTTP handler, astro
scheduler timer).  The internal ``_lock`` guards the running/shots/remaining
counters; reads without the lock are safe in CPython because int/bool
assignment is atomic.
"""

from __future__ import annotations

import threading
import time
from typing import Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from trigger_engine import TriggerEngine


class TimeLapse:
    def __init__(
        self,
        trigger_engine: "TriggerEngine",
        send_response: Callable[[str], None],
    ) -> None:
        self._te    = trigger_engine
        self._send  = send_response
        self._lock  = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

        # Live state
        self._running   = False
        self._shots     = 0
        self._remaining = 0   # seconds left in the session

    # ── Properties ────────────────────────────────────────────────────────────

    @property
    def running(self) -> bool:
        return self._running

    @property
    def shots_taken(self) -> int:
        return self._shots

    @property
    def remaining_sec(self) -> int:
        return max(0, self._remaining)

    # ── Control ───────────────────────────────────────────────────────────────

    def start(self, interval_sec: int, duration_sec: int) -> bool:
        """
        Start a new time-lapse session.

        Returns True if the session started successfully; False if one is
        already running (caller should emit TIMELAPSE_ERR,BUSY).
        """
        with self._lock:
            if self._running:
                return False
            self._running   = True
            self._shots     = 0
            self._remaining = max(1, duration_sec)
            self._stop_event.clear()

        self._thread = threading.Thread(
            target=self._run,
            args=(max(1, interval_sec), max(1, duration_sec)),
            daemon=True,
            name="timelapse",
        )
        self._thread.start()
        print(f"[TimeLapse] Started: interval={interval_sec}s duration={duration_sec}s")
        return True

    def stop(self) -> None:
        """
        Request the running session to stop.  The thread finishes after the
        current shot and emits TIMELAPSE_DONE.
        """
        self._stop_event.set()

    # ── Status ─────────────────────────────────────────────────────────────────

    def status_line(self) -> str:
        """Return a ``TIMELAPSE,<state>,<shots>,<remainingSec>`` response line."""
        state = "running" if self._running else "idle"
        return f"TIMELAPSE,{state},{self._shots},{self.remaining_sec}\n"

    # ── Background loop ────────────────────────────────────────────────────────

    def _run(self, interval_sec: int, duration_sec: int) -> None:
        deadline  = time.monotonic() + duration_sec
        next_shot = time.monotonic()   # fire on the very first tick

        while not self._stop_event.is_set():
            now = time.monotonic()
            self._remaining = max(0, int(deadline - now))

            if now >= deadline:
                break

            if now >= next_shot:
                fired = self._te.fire_manual()
                if fired:
                    with self._lock:
                        self._shots += 1
                    self._send(f"TIMELAPSE_SHOT,{self._shots}\n")
                # Advance by the fixed interval to prevent drift accumulation.
                next_shot += interval_sec
                # If the trigger ran so long that the next slot has already
                # passed, skip the missed fires rather than bunching shots.
                now2 = time.monotonic()
                if next_shot <= now2:
                    skipped = int((now2 - next_shot) / interval_sec) + 1
                    print(
                        f"[TimeLapse] WARNING: {skipped} shot(s) skipped — "
                        f"trigger took longer than the interval"
                    )
                    next_shot += skipped * interval_sec

            # Sleep in small chunks so stop() is responsive (max 0.25 s latency).
            sleep = min(0.25, max(0.0, next_shot - time.monotonic()))
            self._stop_event.wait(timeout=sleep)

        with self._lock:
            self._running   = False
            self._remaining = 0

        self._send("TIMELAPSE_DONE\n")
        print(f"[TimeLapse] Done — {self._shots} shots taken")
