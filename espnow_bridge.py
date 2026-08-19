"""
ESP-NOW bridge serial driver for Pi TX.

Opens /dev/serial0 (Pi hardware UART, GPIO 14/15) at 115200 baud and
communicates with the ESP32 co-processor running pi_espnow_bridge.ino.
All ESP-NOW commands from the app are forwarded to the bridge, and all
radio events (TXBATT, ESPNOW_RX_INFO, ESPNOW_RX_FW, …) from the bridge
are fed into the Pi daemon's outbox so the app receives them unchanged.

Protocol: newline-delimited ASCII text in both directions.

    Pi → Bridge:   ESPNOW_PAIR,AA:BB  TX_TRIGGER,300,0  ...
    Bridge → Pi:   ESPNOW_BRIDGE_READY  TXBATT,<mac>,<pct>  ...

Usage:
    bridge = EspNowBridge(send_response)
    bridge.start()                         # opens /dev/serial0, starts reader
    bridge.send("ESPNOW_GET_MAC\\n")       # forward a command
    bridge.stop()                          # closes port, stops reader

Graceful no-op when /dev/serial0 is absent or pyserial is not installed:
  start() logs a warning and schedules retries.  send() is a silent no-op
  while disconnected.  The app sees ESPNOW_BRIDGE,DISCONNECTED in the outbox
  and the ESP-NOW stub responses remain active in command_parser.py.
"""

import threading
import time
from typing import Callable

_SERIAL_PORT            = "/dev/serial0"
_BAUD_RATE              = 115200
_RECONNECT_INTERVAL_SEC = 5.0
_CONNECT_TIMEOUT_SEC    = 10.0   # max time _try_connect may block before the loop considers it hung
_HANDSHAKE_TIMEOUT_SEC  = 5.0    # max time to wait for ESPNOW_BRIDGE_READY after port open
_BATT_POLL_INTERVAL_SEC = 60.0   # matches ESP32 TX firmware 60 s battery poll


class EspNowBridge:
    """Manages the serial link to the ESP32 ESP-NOW co-processor."""

    def __init__(self, send_response: Callable[[str], None]) -> None:
        """
        send_response: callable that enqueues a response line for the app.
        Typically main.py's send_response(), which adds to the HTTP outbox.
        """
        self._send_response = send_response
        self._serial = None
        self._connected = False
        self._running   = False
        self._write_lock = threading.Lock()  # serialises writes and all connection-state mutations
        self._connect_epoch = 0  # monotonically increasing; guards stale/post-stop workers
        self._connect_worker: threading.Thread | None = None  # at most one in-flight attempt
        self._reader_thread:  threading.Thread | None = None
        self._connect_thread: threading.Thread | None = None
        self._batt_timer: threading.Timer | None = None
        # Optional callback fired when the paired Laser Eye TX triggers over
        # ESP-NOW (ESPNOW_LX_TRIGGER,<pulse_ms> from the bridge).
        self._lx_trigger_cb: Callable[[int], None] | None = None

    def set_lx_trigger_callback(self, cb: Callable[[int], None]) -> None:
        """Register a callback invoked with pulse_ms when the paired
        Laser Eye TX fires a trigger over ESP-NOW."""
        self._lx_trigger_cb = cb

    # ── Public API ────────────────────────────────────────────────────────────

    @property
    def connected(self) -> bool:
        """True when the serial port is open and the reader thread is running."""
        return self._connected

    def start(self) -> None:
        """Start the background connection/retry loop."""
        self._running = True
        self._connect_thread = threading.Thread(
            target=self._connect_loop, daemon=True, name="espnow-connect"
        )
        self._connect_thread.start()

    def stop(self) -> None:
        """Tear down the serial link and stop all background threads.

        Sets _running = False and then, under _write_lock, invalidates the
        current connect epoch and clears all connection state atomically.
        This prevents a concurrent _try_connect from installing a connection
        after stop() has already torn everything down.
        """
        self._running = False
        with self._write_lock:
            self._connect_epoch += 1  # invalidate any in-flight attempt
            # Close and clear connection state atomically so _try_connect
            # cannot set _connected = True after we set it to False here.
            if self._serial is not None:
                try:
                    self._serial.close()
                except Exception:
                    pass
                self._serial = None
            self._connected = False
        self._cancel_batt_timer()
        print("[Bridge] Stopped")

    def send(self, cmd: str) -> None:
        """Send a newline-terminated command to the bridge.

        Silently discarded when the bridge is not connected.
        Thread-safe — may be called from any thread.
        """
        if not self._connected or self._serial is None:
            return
        if not cmd.endswith("\n"):
            cmd += "\n"
        with self._write_lock:
            try:
                self._serial.write(cmd.encode("ascii"))
                self._serial.flush()
            except Exception as exc:
                print(f"[Bridge] Write error: {exc}")
                self._mark_disconnected()

    # ── Background threads ────────────────────────────────────────────────────

    def _connect_loop(self) -> None:
        """Retry connection every _RECONNECT_INTERVAL_SEC until stopped.

        _try_connect is run in a daemon worker thread and joined with
        _CONNECT_TIMEOUT_SEC.  At most one worker is ever in-flight: a new
        attempt is started only after the previous worker has exited.  This
        bounds the thread count to one regardless of how long serial.Serial()
        blocks, while still keeping the loop alive and responsive to stop().

        If a join times out the worker's epoch is invalidated so it discards
        any port it eventually opens.  The loop waits for the same worker on
        the next iteration; a fresh attempt is started once the worker exits.

        All connection-state mutations (_serial, _connected) happen inside
        _write_lock, making them atomic with stop() and preventing races
        between a late-completing worker and a concurrent stop().
        """
        while self._running:
            if not self._connected:
                # Only launch a new worker when the previous one has exited.
                # This bounds outstanding threads to at most one even when
                # serial.Serial() hangs indefinitely.
                if self._connect_worker is None or not self._connect_worker.is_alive():
                    with self._write_lock:
                        self._connect_epoch += 1
                        epoch = self._connect_epoch
                    self._connect_worker = threading.Thread(
                        target=self._try_connect,
                        args=(epoch,),
                        daemon=True,
                        name="espnow-connect-attempt",
                    )
                    self._connect_worker.start()

                # Join the active worker with a timeout; the loop is never
                # blocked longer than _CONNECT_TIMEOUT_SEC regardless of what
                # the worker is doing.
                self._connect_worker.join(timeout=_CONNECT_TIMEOUT_SEC)
                if self._connect_worker.is_alive():
                    # Invalidate the epoch so the hung worker discards any port
                    # it eventually opens; a new attempt starts once it exits.
                    with self._write_lock:
                        if self._connect_epoch == epoch:
                            self._connect_epoch += 1
                    print(
                        f"[Bridge] _try_connect timed out after "
                        f"{_CONNECT_TIMEOUT_SEC:.0f}s — will retry when worker exits"
                    )

            time.sleep(_RECONNECT_INTERVAL_SEC)

    def _try_connect(self, epoch: int) -> None:
        """Open the serial port, verify the bridge with a handshake, then set up the reader.

        After opening the port the method reads lines for up to
        _HANDSHAKE_TIMEOUT_SEC waiting for ESPNOW_BRIDGE_READY.  A timeout
        means a wrong or stale device is on /dev/serial0 (e.g. after USB
        re-enumeration); the port is closed and the connect loop retries as
        normal.  This prevents the bridge from silently treating a ghost
        connection as connected and corrupting ESP-NOW state.

        All connection-state mutations (_serial, _connected) happen inside
        _write_lock, atomically with the epoch/running check.  If the epoch
        has been invalidated (timeout or stop()), any newly opened serial port
        is closed before returning so no resource is leaked.
        """
        try:
            import serial  # pyserial — optional dep
        except ImportError:
            print("[Bridge] pyserial not installed — ESP-NOW bridge unavailable")
            return

        try:
            ser = serial.Serial(_SERIAL_PORT, _BAUD_RATE, timeout=1.0)
        except Exception as exc:
            print(f"[Bridge] Cannot open {_SERIAL_PORT}: {exc}"
                  f" — retrying in {_RECONNECT_INTERVAL_SEC:.0f}s")
            return

        # ── Handshake: wait for ESPNOW_BRIDGE_READY ──────────────────────
        # Poll for the ready announcement before marking ourselves connected.
        # If we time out, close the port — a wrong device or stale baud
        # settings are on the line — and let the reconnect loop retry.
        deadline = time.monotonic() + _HANDSHAKE_TIMEOUT_SEC
        handshake_ok = False
        while time.monotonic() < deadline:
            try:
                raw = ser.readline()
            except Exception as exc:
                print(f"[Bridge] Handshake read error: {exc} — closing port")
                break
            if not raw:
                continue
            line = raw.decode("ascii", errors="replace").strip()
            if line:
                print(f"[Bridge] ← {line}")
            if line == "ESPNOW_BRIDGE_READY":
                handshake_ok = True
                break

        if not handshake_ok:
            print(
                f"[Bridge] Handshake timed out after {_HANDSHAKE_TIMEOUT_SEC:.0f}s"
                f" — closing port and retrying in {_RECONNECT_INTERVAL_SEC:.0f}s"
            )
            try:
                ser.close()
            except Exception:
                pass
            return

        # ── Atomically validate the epoch and install, or discard ─────────
        # Including _connected = True inside the lock ensures stop() cannot
        # clear it after we set it, leaving the bridge falsely connected.
        with self._write_lock:
            if self._connect_epoch != epoch or not self._running:
                # Stale or post-stop: discard the port we just opened.
                try:
                    ser.close()
                except Exception:
                    pass
                return
            self._serial = ser
            self._connected = True

        print(f"[Bridge] Connected to {_SERIAL_PORT} at {_BAUD_RATE} baud")
        # ESPNOW_BRIDGE_READY was consumed by the handshake loop above, so
        # the reader thread will not see it.  Emit ESPNOW_BRIDGE,CONNECTED
        # here so the app can update its bridge-status indicator.
        self._send_response("ESPNOW_BRIDGE,CONNECTED\n")

        # Start reader thread
        reader = threading.Thread(
            target=self._reader_loop, daemon=True, name="espnow-reader"
        )
        self._reader_thread = reader
        reader.start()

        # Request state sync from the newly-connected bridge
        self.send("GET_ESP_MAC")
        self.send("ESPNOW_GET_MAC")

        # Start periodic battery poll
        self._schedule_batt_poll()

    def _reader_loop(self) -> None:
        """Read lines from bridge and route them to the outbox."""
        while self._running and self._connected:
            try:
                raw = self._serial.readline()
                if not raw:
                    continue
                line = raw.decode("ascii", errors="replace").strip()
                if not line:
                    continue
                print(f"[Bridge] ← {line}")
                self._on_event(line)
            except Exception as exc:
                if self._running:
                    print(f"[Bridge] Read error: {exc}")
                break

        self._mark_disconnected()
        print("[Bridge] Reader thread exited")

    # ── Event routing ─────────────────────────────────────────────────────────

    def _on_event(self, line: str) -> None:
        """Route a bridge event line to the HTTP outbox.

        ESPNOW_BRIDGE_READY triggers a state-sync sequence (GET_ESP_MAC /
        ESPNOW_GET_MAC) and emits ESPNOW_BRIDGE,CONNECTED into the outbox so
        the app can update its bridge-status indicator.

        All other lines (TXBATT, ESPNOW_RX_INFO, ESPNOW_RX_FW, ESPNOW_MAC,
        ESPNOW_PAIR,OK, ESP_MAC, …) are forwarded verbatim to the outbox — the
        app parses them the same way it parses the ESP32 TX responses.
        """
        if line == "ESPNOW_BRIDGE_READY":
            # Bridge rebooted or power-cycled — re-sync and notify app
            print("[Bridge] Bridge announced ready — syncing state")
            self.send("GET_ESP_MAC")
            self.send("ESPNOW_GET_MAC")
            self._send_response("ESPNOW_BRIDGE,CONNECTED\n")
            return

        # Laser Eye TX fired over ESP-NOW — route into the trigger engine.
        # The raw line is also forwarded to the outbox (below) so the app
        # can display the event.
        if line.startswith("ESPNOW_LX_TRIGGER,"):
            try:
                pulse_ms = int(line.split(",", 1)[1])
            except (ValueError, IndexError):
                pulse_ms = 0
            print(f"[Bridge] Laser Eye TX trigger received (pulse={pulse_ms} ms)")
            if self._lx_trigger_cb is not None:
                try:
                    self._lx_trigger_cb(pulse_ms)
                except Exception as exc:
                    print(f"[Bridge] LX trigger callback error: {exc}")
        elif line.startswith("ESPNOW_LX_WAKE,"):
            # Wake pulses keep the camera awake via half-press on the wire —
            # the Pi camera is USB-controlled and needs no wake, so just log.
            print(f"[Bridge] Laser Eye TX wake received ({line})")

        # Forward all other events unchanged
        self._send_response(f"{line}\n")

    # ── Periodic battery poll ─────────────────────────────────────────────────

    def _schedule_batt_poll(self) -> None:
        """Schedule a recurring TX_BATT_REQ to all paired RX receivers."""
        if not self._running:
            return
        self._batt_timer = threading.Timer(
            _BATT_POLL_INTERVAL_SEC, self._fire_batt_poll
        )
        self._batt_timer.daemon = True
        self._batt_timer.start()

    def _fire_batt_poll(self) -> None:
        if not self._running or not self._connected:
            return
        self.send("TX_BATT_REQ")
        self._schedule_batt_poll()  # reschedule

    def _cancel_batt_timer(self) -> None:
        if self._batt_timer is not None:
            self._batt_timer.cancel()
            self._batt_timer = None

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _mark_disconnected(self) -> None:
        """Called on read/write failure; notifies app and arms reconnect."""
        if not self._connected:
            return
        self._connected = False
        self._cancel_batt_timer()
        try:
            if self._serial:
                self._serial.close()
        except Exception:
            pass
        self._serial = None
        print("[Bridge] Disconnected")
        self._send_response("ESPNOW_BRIDGE,DISCONNECTED\n")
