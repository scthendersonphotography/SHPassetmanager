"""
Unit tests for EspNowBridge — serial reconnect and event routing.

All tests run without Pi hardware: the serial port is mocked so no
/dev/serial0, pyserial, or GPIO is required.  Tests verify:

  TestDisconnectEvent
    - _mark_disconnected emits ESPNOW_BRIDGE,DISCONNECTED in outbox
    - _mark_disconnected is idempotent (second call does nothing)
    - _mark_disconnected cancels the battery-poll timer

  TestReconnectEvent
    - _on_event(ESPNOW_BRIDGE_READY) emits ESPNOW_BRIDGE,CONNECTED
    - _on_event(ESPNOW_BRIDGE_READY) sends GET_ESP_MAC to bridge
    - _on_event(ESPNOW_BRIDGE_READY) sends ESPNOW_GET_MAC to bridge

  TestSendNoOpWhileDisconnected
    - send() is silent while _connected is False — no exception raised
    - send() does not call serial.write when disconnected

  TestStateSyncOnConnect
    - _try_connect sends GET_ESP_MAC after opening the port
    - _try_connect sends ESPNOW_GET_MAC after opening the port

  TestBatteryPoll
    - _fire_batt_poll sends TX_BATT_REQ when connected
    - _fire_batt_poll is a no-op when disconnected
    - _fire_batt_poll reschedules itself after firing
    - battery timer is cancelled by _mark_disconnected
"""

import itertools
import sys
import threading
import time
import types
import unittest
from unittest.mock import MagicMock, call, patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_bridge(connected=True):
    """Return an EspNowBridge with a mock serial port, plus a list that
    collects every outbox line the bridge emits."""
    # Inject a stub 'serial' module so pyserial is not required.
    fake_serial_module = types.ModuleType("serial")
    fake_serial_instance = MagicMock()
    fake_serial_module.Serial = MagicMock(return_value=fake_serial_instance)
    sys.modules.setdefault("serial", fake_serial_module)

    from espnow_bridge import EspNowBridge

    outbox: list[str] = []
    bridge = EspNowBridge(send_response=outbox.append)
    bridge._running = True

    if connected:
        bridge._serial = fake_serial_instance
        bridge._connected = True

    return bridge, outbox, fake_serial_instance


# ---------------------------------------------------------------------------
# TestDisconnectEvent
# ---------------------------------------------------------------------------

class TestDisconnectEvent(unittest.TestCase):
    """_mark_disconnected must notify the app and clean up state."""

    def test_emits_disconnected_to_outbox(self):
        bridge, outbox, _ = _make_bridge(connected=True)
        bridge._mark_disconnected()
        self.assertIn(
            "ESPNOW_BRIDGE,DISCONNECTED\n", outbox,
            "Outbox must receive ESPNOW_BRIDGE,DISCONNECTED when link drops",
        )

    def test_connected_flag_cleared(self):
        bridge, _, _ = _make_bridge(connected=True)
        bridge._mark_disconnected()
        self.assertFalse(bridge.connected,
            "_connected must be False after _mark_disconnected")

    def test_idempotent_second_call_does_not_double_emit(self):
        """Calling _mark_disconnected twice must not queue two DISCONNECTED events."""
        bridge, outbox, _ = _make_bridge(connected=True)
        bridge._mark_disconnected()
        bridge._mark_disconnected()
        disconnected_count = outbox.count("ESPNOW_BRIDGE,DISCONNECTED\n")
        self.assertEqual(disconnected_count, 1,
            "ESPNOW_BRIDGE,DISCONNECTED must be emitted exactly once per disconnect")

    def test_battery_timer_cancelled_on_disconnect(self):
        """The battery-poll timer must be cancelled when the link drops."""
        bridge, _, _ = _make_bridge(connected=True)
        # Arm a timer that we can inspect
        mock_timer = MagicMock()
        bridge._batt_timer = mock_timer
        bridge._mark_disconnected()
        mock_timer.cancel.assert_called_once()
        self.assertIsNone(bridge._batt_timer,
            "_batt_timer reference must be cleared after cancel")


# ---------------------------------------------------------------------------
# TestReconnectEvent
# ---------------------------------------------------------------------------

class TestReconnectEvent(unittest.TestCase):
    """ESPNOW_BRIDGE_READY must trigger state-sync and notify the app."""

    def test_emits_connected_to_outbox(self):
        bridge, outbox, serial_inst = _make_bridge(connected=True)
        bridge._on_event("ESPNOW_BRIDGE_READY")
        self.assertIn(
            "ESPNOW_BRIDGE,CONNECTED\n", outbox,
            "Outbox must receive ESPNOW_BRIDGE,CONNECTED on ESPNOW_BRIDGE_READY",
        )

    def test_sends_get_esp_mac_to_bridge(self):
        bridge, _, serial_inst = _make_bridge(connected=True)
        bridge._on_event("ESPNOW_BRIDGE_READY")
        written_lines = [
            call_args[0][0].decode("ascii", errors="replace").strip()
            for call_args in serial_inst.write.call_args_list
        ]
        self.assertIn("GET_ESP_MAC", written_lines,
            "GET_ESP_MAC must be sent to the bridge after ESPNOW_BRIDGE_READY")

    def test_sends_espnow_get_mac_to_bridge(self):
        bridge, _, serial_inst = _make_bridge(connected=True)
        bridge._on_event("ESPNOW_BRIDGE_READY")
        written_lines = [
            call_args[0][0].decode("ascii", errors="replace").strip()
            for call_args in serial_inst.write.call_args_list
        ]
        self.assertIn("ESPNOW_GET_MAC", written_lines,
            "ESPNOW_GET_MAC must be sent to the bridge after ESPNOW_BRIDGE_READY")

    def test_ready_event_not_forwarded_verbatim(self):
        """ESPNOW_BRIDGE_READY must NOT appear in the outbox as a raw line."""
        bridge, outbox, _ = _make_bridge(connected=True)
        bridge._on_event("ESPNOW_BRIDGE_READY")
        self.assertNotIn("ESPNOW_BRIDGE_READY\n", outbox,
            "ESPNOW_BRIDGE_READY must be consumed, not forwarded to the app verbatim")

    def test_other_events_forwarded_verbatim(self):
        """Non-READY events must be forwarded to the outbox unchanged."""
        bridge, outbox, _ = _make_bridge(connected=True)
        bridge._on_event("TXBATT,AA:BB:CC:DD:EE:FF,87")
        self.assertIn("TXBATT,AA:BB:CC:DD:EE:FF,87\n", outbox,
            "TXBATT events must be forwarded verbatim to the outbox")


# ---------------------------------------------------------------------------
# TestSendNoOpWhileDisconnected
# ---------------------------------------------------------------------------

class TestSendNoOpWhileDisconnected(unittest.TestCase):
    """send() must be a silent no-op when the bridge link is down."""

    def test_no_exception_when_disconnected(self):
        bridge, _, _ = _make_bridge(connected=False)
        # Must not raise
        try:
            bridge.send("TX_TRIGGER,300,0")
        except Exception as exc:
            self.fail(f"send() raised an exception while disconnected: {exc}")

    def test_serial_write_not_called_when_disconnected(self):
        bridge, _, serial_inst = _make_bridge(connected=False)
        bridge.send("TX_TRIGGER,300,0")
        serial_inst.write.assert_not_called()

    def test_send_works_normally_when_connected(self):
        bridge, _, serial_inst = _make_bridge(connected=True)
        bridge.send("TX_TRIGGER,300,0")
        serial_inst.write.assert_called_once()
        written = serial_inst.write.call_args[0][0]
        self.assertIn(b"TX_TRIGGER,300,0", written)

    def test_send_appends_newline(self):
        """send() must terminate the command with \\n if absent."""
        bridge, _, serial_inst = _make_bridge(connected=True)
        bridge.send("ESPNOW_GET_MAC")  # no trailing newline
        written = serial_inst.write.call_args[0][0]
        self.assertTrue(written.endswith(b"\n"),
            "send() must append \\n when the command has none")


# ---------------------------------------------------------------------------
# TestStateSyncOnConnect
# ---------------------------------------------------------------------------

class TestStateSyncOnConnect(unittest.TestCase):
    """_try_connect must open the port and issue state-sync commands."""

    def _try_connect_with_mock_serial(self):
        """Run _try_connect with a mocked serial.Serial and return written lines."""
        fake_serial_module = types.ModuleType("serial")
        fake_serial_instance = MagicMock()
        # Return ESPNOW_BRIDGE_READY first so the handshake succeeds, then
        # return empty bytes for subsequent reads by the reader thread.
        fake_serial_instance.readline.side_effect = itertools.chain(
            [b"ESPNOW_BRIDGE_READY\n"], itertools.repeat(b"")
        )
        fake_serial_module.Serial = MagicMock(return_value=fake_serial_instance)

        # Temporarily replace or inject the serial module
        original = sys.modules.get("serial")
        sys.modules["serial"] = fake_serial_module
        try:
            # Re-import to pick up the mock (module may already be cached)
            import espnow_bridge
            outbox: list[str] = []
            bridge = espnow_bridge.EspNowBridge(send_response=outbox.append)
            bridge._running = True
            # epoch must match _connect_epoch for _try_connect to install
            epoch = 1
            bridge._connect_epoch = epoch

            # Patch time.sleep so _try_connect's 0.5 s wait is instant
            with patch("time.sleep"):
                bridge._try_connect(epoch)

            written_lines = [
                c[0][0].decode("ascii", errors="replace").strip()
                for c in fake_serial_instance.write.call_args_list
            ]
            return bridge, outbox, written_lines
        finally:
            if original is None:
                sys.modules.pop("serial", None)
            else:
                sys.modules["serial"] = original

    def test_connected_flag_set_after_connect(self):
        bridge, _, _ = self._try_connect_with_mock_serial()
        self.assertTrue(bridge.connected,
            "_connected must be True immediately after _try_connect succeeds")

    def test_get_esp_mac_sent_on_connect(self):
        _, _, written = self._try_connect_with_mock_serial()
        self.assertIn("GET_ESP_MAC", written,
            "GET_ESP_MAC must be sent after the serial port is opened")

    def test_espnow_get_mac_sent_on_connect(self):
        _, _, written = self._try_connect_with_mock_serial()
        self.assertIn("ESPNOW_GET_MAC", written,
            "ESPNOW_GET_MAC must be sent after the serial port is opened")


# ---------------------------------------------------------------------------
# TestBatteryPoll
# ---------------------------------------------------------------------------

class TestBatteryPoll(unittest.TestCase):
    """Periodic TX_BATT_REQ must fire reliably and respect connection state."""

    def test_fire_batt_poll_sends_tx_batt_req_when_connected(self):
        bridge, _, serial_inst = _make_bridge(connected=True)
        # Cancel auto-rescheduled timer to keep the test synchronous
        with patch.object(bridge, "_schedule_batt_poll"):
            bridge._fire_batt_poll()
        written_lines = [
            c[0][0].decode("ascii", errors="replace").strip()
            for c in serial_inst.write.call_args_list
        ]
        self.assertIn("TX_BATT_REQ", written_lines,
            "TX_BATT_REQ must be sent by the battery poll when connected")

    def test_fire_batt_poll_no_op_when_disconnected(self):
        bridge, _, serial_inst = _make_bridge(connected=False)
        bridge._fire_batt_poll()
        serial_inst.write.assert_not_called()

    def test_fire_batt_poll_no_op_when_stopped(self):
        bridge, _, serial_inst = _make_bridge(connected=True)
        bridge._running = False
        bridge._fire_batt_poll()
        serial_inst.write.assert_not_called()

    def test_fire_batt_poll_reschedules_itself(self):
        """After firing, the poll must arm the next timer."""
        bridge, _, _ = _make_bridge(connected=True)
        with patch.object(bridge, "_schedule_batt_poll") as mock_schedule:
            bridge._fire_batt_poll()
        mock_schedule.assert_called_once()

    def test_battery_timer_cancelled_on_disconnect(self):
        """_mark_disconnected must cancel a live battery timer."""
        bridge, _, _ = _make_bridge(connected=True)
        fired: list[bool] = []

        # Install a real Timer so we can test it is actually cancelled
        def _fake_poll():
            fired.append(True)

        real_timer = threading.Timer(0.2, _fake_poll)
        bridge._batt_timer = real_timer
        real_timer.start()

        bridge._mark_disconnected()

        # Wait long enough for the timer to have fired if not cancelled
        time.sleep(0.35)
        self.assertEqual(fired, [],
            "Battery timer must be cancelled by _mark_disconnected before it fires")

    def test_schedule_batt_poll_does_nothing_when_stopped(self):
        """_schedule_batt_poll must not arm a timer after stop()."""
        bridge, _, _ = _make_bridge(connected=True)
        bridge._running = False
        bridge._schedule_batt_poll()
        self.assertIsNone(bridge._batt_timer,
            "_batt_timer must not be set when _running is False")


# ---------------------------------------------------------------------------
# TestConnectLoopSleep — reconnect loop must always sleep between attempts
# ---------------------------------------------------------------------------

class TestConnectLoopSleep(unittest.TestCase):
    """_connect_loop must sleep _RECONNECT_INTERVAL_SEC even on fast failures.

    If _try_connect raises or returns quickly (e.g. port absent), the loop
    must still pause before retrying so it cannot spin at 100% CPU.
    """

    def _run_loop_n_times(self, n: int):
        """Run _connect_loop for exactly n iterations and return sleep durations."""
        from espnow_bridge import EspNowBridge

        outbox: list[str] = []
        bridge = EspNowBridge(send_response=outbox.append)
        bridge._running = True
        bridge._connected = False

        iterations: list[int] = [0]
        sleep_args: list[float] = []

        def fake_sleep(secs: float) -> None:
            sleep_args.append(secs)
            iterations[0] += 1
            if iterations[0] >= n:
                bridge._running = False  # stop after n sleeps

        with patch.object(bridge, "_try_connect", return_value=None), \
             patch("espnow_bridge.time.sleep", side_effect=fake_sleep):
            bridge._connect_loop()

        return sleep_args

    def test_sleeps_between_fast_failures(self):
        """Loop must call time.sleep after every fast-failing _try_connect."""
        from espnow_bridge import _RECONNECT_INTERVAL_SEC

        sleep_args = self._run_loop_n_times(3)

        self.assertEqual(len(sleep_args), 3,
            "_connect_loop must sleep once per iteration; expected 3 sleeps for 3 iterations")

    def test_sleep_duration_equals_reconnect_interval(self):
        """Each sleep must use exactly _RECONNECT_INTERVAL_SEC."""
        from espnow_bridge import _RECONNECT_INTERVAL_SEC

        sleep_args = self._run_loop_n_times(3)

        for i, secs in enumerate(sleep_args):
            self.assertAlmostEqual(secs, _RECONNECT_INTERVAL_SEC, places=6,
                msg=f"Sleep #{i + 1} must be _RECONNECT_INTERVAL_SEC "
                    f"({_RECONNECT_INTERVAL_SEC}s) but got {secs}s")

    def test_sleep_never_less_than_one_second(self):
        """Minimum sleep must be >= 1 s to prevent CPU saturation."""
        sleep_args = self._run_loop_n_times(3)

        for i, secs in enumerate(sleep_args):
            self.assertGreaterEqual(secs, 1.0,
                f"Sleep #{i + 1} must be >= 1 s to prevent CPU spin on "
                f"fast failures (got {secs}s)")




# ---------------------------------------------------------------------------
# TestConnectLoopTimeout — hung _try_connect must not stall the retry loop
# ---------------------------------------------------------------------------

class TestConnectLoopTimeout(unittest.TestCase):
    """_connect_loop must resume and retry even when _try_connect blocks forever.

    The loop runs _try_connect in a worker thread and joins it with
    _CONNECT_TIMEOUT_SEC.  If the worker is still alive after the join, the
    loop logs a warning and moves on to the next sleep-and-retry cycle.  This
    test patches _CONNECT_TIMEOUT_SEC to 50 ms and blocks _try_connect with a
    threading.Event so the worker truly hangs regardless of time.sleep patches.
    """

    def _make_hanging_bridge(self):
        """Return (bridge, hang_event) where _try_connect blocks on hang_event."""
        import espnow_bridge

        outbox: list[str] = []
        bridge = espnow_bridge.EspNowBridge(send_response=outbox.append)
        bridge._running = True
        bridge._connected = False
        hang_event = threading.Event()
        return bridge, hang_event

    def test_loop_resumes_after_hung_try_connect(self):
        """_connect_loop must call time.sleep even when _try_connect never returns."""
        import espnow_bridge

        bridge, hang_event = self._make_hanging_bridge()
        sleep_calls: list[float] = []

        def hanging_try_connect(epoch):  # epoch passed by the loop
            hang_event.wait()  # blocks via threading.Event — unaffected by time.sleep patch

        def fake_sleep(secs: float) -> None:
            sleep_calls.append(secs)
            bridge._running = False  # stop after one full iteration

        with patch.object(bridge, "_try_connect", side_effect=hanging_try_connect), \
             patch("espnow_bridge.time.sleep", side_effect=fake_sleep), \
             patch("espnow_bridge._CONNECT_TIMEOUT_SEC", 0.05):
            bridge._connect_loop()

        hang_event.set()  # release daemon worker so it can exit
        self.assertGreaterEqual(
            len(sleep_calls), 1,
            "_connect_loop must call time.sleep after a hung _try_connect, "
            "proving it did not block indefinitely",
        )

    def test_loop_logs_timeout_warning(self):
        """_connect_loop must print a timeout warning when _try_connect hangs."""
        import espnow_bridge

        bridge, hang_event = self._make_hanging_bridge()

        def hanging_try_connect(epoch):
            hang_event.wait()

        def fake_sleep(_secs: float) -> None:
            bridge._running = False

        with patch.object(bridge, "_try_connect", side_effect=hanging_try_connect), \
             patch("espnow_bridge.time.sleep", side_effect=fake_sleep), \
             patch("espnow_bridge._CONNECT_TIMEOUT_SEC", 0.05), \
             patch("builtins.print") as mock_print:
            bridge._connect_loop()

        hang_event.set()
        printed = " ".join(
            str(a) for call_obj in mock_print.call_args_list for a in call_obj.args
        )
        self.assertIn(
            "timed out", printed.lower(),
            "_connect_loop must log a timeout warning when _try_connect hangs",
        )

    def test_loop_retries_after_worker_exits(self):
        """Once a timed-out worker exits, the loop must launch a fresh attempt."""
        import espnow_bridge

        bridge, _ = self._make_hanging_bridge()
        attempt_count: list[int] = [0]

        def hanging_try_connect(epoch):
            attempt_count[0] += 1
            if attempt_count[0] == 1:
                # First attempt: hang briefly (< timeout) so the join succeeds
                # and the worker exits cleanly before the loop moves on.
                threading.Event().wait(timeout=0.02)  # real wait, patch does not affect this

        sleep_count: list[int] = [0]

        def fake_sleep(_secs: float) -> None:
            sleep_count[0] += 1
            if sleep_count[0] >= 2:
                bridge._running = False

        with patch.object(bridge, "_try_connect", side_effect=hanging_try_connect), \
             patch("espnow_bridge.time.sleep", side_effect=fake_sleep), \
             patch("espnow_bridge._CONNECT_TIMEOUT_SEC", 0.05):
            bridge._connect_loop()

        self.assertGreaterEqual(
            attempt_count[0], 2,
            "_connect_loop must start a fresh attempt once the previous worker "
            f"exits, but only saw {attempt_count[0]} attempt(s)",
        )

    def test_at_most_one_worker_while_hung(self):
        """The loop must never spawn a second worker while the first is still alive."""
        import espnow_bridge

        bridge, hang_event = self._make_hanging_bridge()
        worker_thread_ids: set = set()

        def hanging_try_connect(epoch):
            worker_thread_ids.add(threading.current_thread().ident)
            hang_event.wait()  # hangs until released

        sleep_count: list[int] = [0]

        def fake_sleep(_secs: float) -> None:
            sleep_count[0] += 1
            if sleep_count[0] >= 3:  # run through 3 loop iterations
                bridge._running = False

        with patch.object(bridge, "_try_connect", side_effect=hanging_try_connect), \
             patch("espnow_bridge.time.sleep", side_effect=fake_sleep), \
             patch("espnow_bridge._CONNECT_TIMEOUT_SEC", 0.05):
            bridge._connect_loop()

        hang_event.set()  # release the daemon worker so it can exit

        self.assertEqual(
            len(worker_thread_ids), 1,
            "Only one worker thread must exist while _try_connect is perpetually "
            f"hung (saw {len(worker_thread_ids)} distinct thread ids over 3 iterations)",
        )

    # -- Epoch / generation guard tests ----------------------------------------

    def _make_serial_mock(self):
        """Return (fake_module, fake_instance) for injecting into sys.modules."""
        fake_serial_module = types.ModuleType("serial")
        fake_serial_instance = MagicMock()
        # Return ESPNOW_BRIDGE_READY first so the handshake succeeds, then
        # return empty bytes for subsequent reads by the reader thread.
        fake_serial_instance.readline.side_effect = itertools.chain(
            [b"ESPNOW_BRIDGE_READY\n"], itertools.repeat(b"")
        )
        fake_serial_module.Serial = MagicMock(return_value=fake_serial_instance)
        return fake_serial_module, fake_serial_instance

    def _run_try_connect(self, epoch, bridge_epoch, running=True):
        """
        Call the real _try_connect(epoch) against a mock serial port.
        bridge._connect_epoch is set to bridge_epoch before the call.
        Returns (bridge, fake_serial_instance).
        """
        fake_serial_module, fake_serial_instance = self._make_serial_mock()
        original = sys.modules.get("serial")
        sys.modules["serial"] = fake_serial_module
        try:
            import espnow_bridge
            outbox: list[str] = []
            bridge = espnow_bridge.EspNowBridge(send_response=outbox.append)
            bridge._running = running
            bridge._connect_epoch = bridge_epoch
            with patch("time.sleep"):
                bridge._try_connect(epoch)
            return bridge, fake_serial_instance
        finally:
            if original is None:
                sys.modules.pop("serial", None)
            else:
                sys.modules["serial"] = original

    def test_stale_epoch_does_not_install_connection(self):
        """A late-completing worker with a stale epoch must not set _connected."""
        # epoch=1 but bridge_epoch has moved on to 2 (timeout invalidated it)
        bridge, _ = self._run_try_connect(epoch=1, bridge_epoch=2)
        self.assertFalse(
            bridge.connected,
            "_try_connect with a stale epoch must not mark the bridge connected",
        )

    def test_stale_epoch_closes_opened_port(self):
        """A late-completing worker must close any port it opened."""
        _, fake_ser = self._run_try_connect(epoch=1, bridge_epoch=2)
        fake_ser.close.assert_called_once_with()

    def test_stopped_bridge_does_not_install_connection(self):
        """_try_connect must not install a connection after stop()."""
        # epoch matches, but _running is False
        bridge, _ = self._run_try_connect(epoch=1, bridge_epoch=1, running=False)
        self.assertFalse(
            bridge.connected,
            "_try_connect must not mark the bridge connected after stop()",
        )

    def test_stopped_bridge_closes_opened_port(self):
        """_try_connect must close the port it opened if the bridge was stopped."""
        _, fake_ser = self._run_try_connect(epoch=1, bridge_epoch=1, running=False)
        fake_ser.close.assert_called_once_with()

    def test_valid_epoch_installs_connection(self):
        """When epoch matches and bridge is running, _try_connect must connect."""
        bridge, _ = self._run_try_connect(epoch=1, bridge_epoch=1, running=True)
        self.assertTrue(
            bridge.connected,
            "_try_connect must mark the bridge connected when epoch is valid",
        )

    def test_stop_after_connect_leaves_bridge_disconnected(self):
        """stop() called after a successful _try_connect must leave _connected False."""
        # Run a successful connect so bridge._connected = True
        bridge, _ = self._run_try_connect(epoch=1, bridge_epoch=1, running=True)
        self.assertTrue(bridge.connected, "pre-condition: bridge must be connected")

        bridge.stop()

        self.assertFalse(
            bridge.connected,
            "stop() must set _connected = False even when called immediately "
            "after a successful _try_connect",
        )

    def test_concurrent_stop_invalidates_install(self):
        """stop() must prevent _try_connect from leaving the bridge connected.

        This exercises the critical section that makes _serial and _connected
        mutations atomic with epoch invalidation in stop().  We simulate the
        race by calling stop() between the moment _try_connect opens the port
        and the moment it checks the epoch, by pre-invalidating the epoch
        (simulating what stop() does) and verifying _try_connect discards
        the port instead of installing it.
        """
        fake_serial_module, fake_serial_instance = self._make_serial_mock()
        original = sys.modules.get("serial")
        sys.modules["serial"] = fake_serial_module
        try:
            import espnow_bridge

            outbox: list[str] = []
            bridge = espnow_bridge.EspNowBridge(send_response=outbox.append)
            bridge._running = False  # stop() has already run
            # stop() bumps the epoch; epoch=1 is now stale
            bridge._connect_epoch = 2

            with patch("time.sleep"):
                bridge._try_connect(epoch=1)

            self.assertFalse(
                bridge.connected,
                "A _try_connect attempt that races with stop() must not leave "
                "the bridge connected",
            )
            fake_serial_instance.close.assert_called_once_with()
        finally:
            if original is None:
                sys.modules.pop("serial", None)
            else:
                sys.modules["serial"] = original


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# TestHandshakeTimeout — _try_connect must verify ESPNOW_BRIDGE_READY
# ---------------------------------------------------------------------------

class TestHandshakeTimeout(unittest.TestCase):
    """_try_connect must close the port and abort when the handshake times out.

    After opening /dev/serial0 the bridge waits up to _HANDSHAKE_TIMEOUT_SEC
    for an ESPNOW_BRIDGE_READY line.  If the line never arrives (wrong device,
    stale baud settings, or bridge still booting) the port must be closed and
    _connected must remain False so the reconnect loop retries cleanly.

    Tests:
      - Timeout leaves _connected False
      - Timeout closes the opened port
      - Timeout logs a message containing "timed out"
      - Handshake error (readline raises) leaves _connected False
      - Handshake error closes the opened port
      - Non-READY lines before READY are ignored; READY still succeeds
      - Successful handshake sets _connected True
      - Successful handshake emits ESPNOW_BRIDGE,CONNECTED to the outbox
    """

    def _make_timeout_bridge(self, readline_side_effect):
        """
        Build a bridge whose serial readline always returns empty bytes
        (simulating no ESPNOW_BRIDGE_READY arriving), patch the handshake
        timeout to a very small value, and run _try_connect synchronously.

        Returns (bridge, outbox, fake_serial_instance).
        """
        fake_serial_module = types.ModuleType("serial")
        fake_serial_instance = MagicMock()
        fake_serial_instance.readline.side_effect = readline_side_effect
        fake_serial_module.Serial = MagicMock(return_value=fake_serial_instance)

        original = sys.modules.get("serial")
        sys.modules["serial"] = fake_serial_module
        try:
            import espnow_bridge
            outbox: list[str] = []
            bridge = espnow_bridge.EspNowBridge(send_response=outbox.append)
            bridge._running = True
            epoch = 1
            bridge._connect_epoch = epoch

            with patch("espnow_bridge._HANDSHAKE_TIMEOUT_SEC", 0.05):
                bridge._try_connect(epoch)

            return bridge, outbox, fake_serial_instance
        finally:
            if original is None:
                sys.modules.pop("serial", None)
            else:
                sys.modules["serial"] = original

    def test_timeout_leaves_bridge_disconnected(self):
        """When no ESPNOW_BRIDGE_READY arrives, _connected must remain False."""
        bridge, _, _ = self._make_timeout_bridge(readline_side_effect=itertools.repeat(b""))
        self.assertFalse(
            bridge.connected,
            "_try_connect must not mark the bridge connected when the handshake times out",
        )

    def test_timeout_closes_opened_port(self):
        """The port opened before the timeout must be closed before returning."""
        _, _, fake_ser = self._make_timeout_bridge(readline_side_effect=itertools.repeat(b""))
        fake_ser.close.assert_called_once_with()

    def test_timeout_logs_timed_out(self):
        """A handshake timeout must print a message containing 'timed out'."""
        with patch("builtins.print") as mock_print:
            self._make_timeout_bridge(readline_side_effect=itertools.repeat(b""))

        printed = " ".join(
            str(a) for call_obj in mock_print.call_args_list for a in call_obj.args
        )
        self.assertIn(
            "timed out", printed.lower(),
            "_try_connect must log a timeout message when the handshake does not complete",
        )

    def test_handshake_readline_error_leaves_bridge_disconnected(self):
        """If readline() raises during the handshake, _connected must remain False."""
        bridge, _, _ = self._make_timeout_bridge(
            readline_side_effect=OSError("device gone")
        )
        self.assertFalse(
            bridge.connected,
            "_try_connect must not mark the bridge connected when handshake readline raises",
        )

    def test_handshake_readline_error_closes_opened_port(self):
        """If readline() raises during the handshake, the port must be closed."""
        _, _, fake_ser = self._make_timeout_bridge(
            readline_side_effect=OSError("device gone")
        )
        fake_ser.close.assert_called_once_with()

    def test_non_ready_lines_before_ready_are_ignored(self):
        """Lines other than ESPNOW_BRIDGE_READY must be skipped; READY still succeeds."""
        # Feed some unrelated lines before the real READY
        noise = [b"SOME_OTHER_LINE\n", b"TXBATT,AA:BB:CC:DD:EE:FF,72\n"]
        side_effect = itertools.chain(noise, [b"ESPNOW_BRIDGE_READY\n"], itertools.repeat(b""))
        bridge, _, _ = self._make_timeout_bridge(readline_side_effect=side_effect)
        self.assertTrue(
            bridge.connected,
            "_try_connect must connect when ESPNOW_BRIDGE_READY arrives after unrelated lines",
        )

    def test_successful_handshake_sets_connected(self):
        """A bridge that sends ESPNOW_BRIDGE_READY must result in _connected True."""
        side_effect = itertools.chain([b"ESPNOW_BRIDGE_READY\n"], itertools.repeat(b""))
        bridge, _, _ = self._make_timeout_bridge(readline_side_effect=side_effect)
        self.assertTrue(
            bridge.connected,
            "_try_connect must mark the bridge connected when the handshake succeeds",
        )

    def test_successful_handshake_emits_connected_to_outbox(self):
        """After a successful handshake, ESPNOW_BRIDGE,CONNECTED must reach the outbox."""
        side_effect = itertools.chain([b"ESPNOW_BRIDGE_READY\n"], itertools.repeat(b""))
        _, outbox, _ = self._make_timeout_bridge(readline_side_effect=side_effect)
        self.assertIn(
            "ESPNOW_BRIDGE,CONNECTED\n",
            outbox,
            "_try_connect must emit ESPNOW_BRIDGE,CONNECTED after a successful handshake "
            "(ESPNOW_BRIDGE_READY was consumed by the handshake loop and won't reach _on_event)",
        )


# ---------------------------------------------------------------------------
# TestReaderLoopHang — reader must exit cleanly even when readline() hangs
# ---------------------------------------------------------------------------

class TestReaderLoopHang(unittest.TestCase):
    """_reader_loop must exit and call _mark_disconnected when readline() hangs.

    The serial port is opened with timeout=1.0 so readline() returns empty bytes
    after at most 1 second on a hot-unplugged device.  When the OS-level read
    truly stalls (a known kernel edge case on sudden USB removal), the loop
    relies on the serial timeout to unblock and then re-checks _running /
    _connected on the next iteration.

    These tests verify every path:
      - OSError / SerialException from readline → reader exits, _mark_disconnected called
      - readline returns b"" repeatedly (timeout path) → reader exits when _running cleared
      - readline blocks briefly then raises → reader exits within a bounded time
    """

    def test_reader_exits_on_readline_oserror(self):
        """When readline() raises OSError (hot-unplug), the reader must exit."""
        bridge, outbox, serial_inst = _make_bridge(connected=True)
        serial_inst.readline.side_effect = OSError("device disconnected")

        reader = threading.Thread(target=bridge._reader_loop, daemon=True)
        reader.start()
        reader.join(timeout=2.0)

        self.assertFalse(
            reader.is_alive(),
            "Reader thread must exit after readline() raises OSError",
        )

    def test_mark_disconnected_called_on_readline_oserror(self):
        """_mark_disconnected must be called when readline() raises OSError."""
        bridge, outbox, serial_inst = _make_bridge(connected=True)
        serial_inst.readline.side_effect = OSError("USB gone")

        reader = threading.Thread(target=bridge._reader_loop, daemon=True)
        reader.start()
        reader.join(timeout=2.0)

        self.assertIn(
            "ESPNOW_BRIDGE,DISCONNECTED\n",
            outbox,
            "ESPNOW_BRIDGE,DISCONNECTED must be emitted after readline() raises OSError",
        )

    def test_reader_exits_when_running_cleared_between_empty_reads(self):
        """Reader must exit within bounded time when _running is cleared.

        Simulates the serial timeout=1.0 path: readline() returns b"" repeatedly
        (as it would on a stalled device), and the loop exits when _running is
        set to False after a few iterations.
        """
        bridge, outbox, serial_inst = _make_bridge(connected=True)
        unblock = threading.Event()

        def fake_readline():
            # Simulate a serial-timeout-bounded read: blocks briefly then returns empty.
            unblock.wait(timeout=0.05)
            return b""

        serial_inst.readline.side_effect = fake_readline

        reader = threading.Thread(target=bridge._reader_loop, daemon=True)
        reader.start()

        # Let the reader run a couple of timeout cycles, then signal stop.
        time.sleep(0.15)
        bridge._running = False
        unblock.set()

        reader.join(timeout=2.0)
        self.assertFalse(
            reader.is_alive(),
            "Reader thread must exit within 2 s once _running is set to False",
        )

    def test_mark_disconnected_called_after_running_cleared(self):
        """_mark_disconnected must be called after the reader exits via _running=False."""
        bridge, outbox, serial_inst = _make_bridge(connected=True)
        unblock = threading.Event()

        def fake_readline():
            unblock.wait(timeout=0.05)
            return b""

        serial_inst.readline.side_effect = fake_readline

        reader = threading.Thread(target=bridge._reader_loop, daemon=True)
        reader.start()
        time.sleep(0.15)
        bridge._running = False
        unblock.set()
        reader.join(timeout=2.0)

        self.assertIn(
            "ESPNOW_BRIDGE,DISCONNECTED\n",
            outbox,
            "_mark_disconnected must be called when the reader exits due to _running=False",
        )

    def test_reader_exits_when_connected_cleared_between_empty_reads(self):
        """Reader must exit when _connected is cleared, even without _running change."""
        bridge, outbox, serial_inst = _make_bridge(connected=True)
        call_count = [0]
        unblock = threading.Event()

        def fake_readline():
            call_count[0] += 1
            unblock.wait(timeout=0.05)
            if call_count[0] >= 2:
                # After a couple of empty reads, simulate device vanishing
                bridge._connected = False
            return b""

        serial_inst.readline.side_effect = fake_readline

        reader = threading.Thread(target=bridge._reader_loop, daemon=True)
        reader.start()
        unblock.set()
        reader.join(timeout=2.0)

        self.assertFalse(
            reader.is_alive(),
            "Reader thread must exit when _connected is cleared between empty reads",
        )

    def test_reader_exits_after_blocking_readline_raises(self):
        """Reader must exit within bounded time when a blocking readline eventually raises.

        This is the core hang scenario: readline() is in-progress (OS blocked)
        when the device is removed.  The serial timeout=1.0 eventually fires and
        the OS raises an I/O exception.  We simulate this with a brief Event wait
        followed by an OSError.
        """
        bridge, outbox, serial_inst = _make_bridge(connected=True)
        unblock = threading.Event()

        def blocking_then_raise():
            # Simulate OS-level hang bounded by serial timeout, then error.
            unblock.wait(timeout=0.15)
            raise OSError("Input/output error")

        serial_inst.readline.side_effect = blocking_then_raise

        reader = threading.Thread(target=bridge._reader_loop, daemon=True)
        reader.start()
        unblock.set()
        reader.join(timeout=2.0)

        self.assertFalse(
            reader.is_alive(),
            "Reader thread must exit within 2 s after a blocking readline() raises",
        )
        self.assertIn(
            "ESPNOW_BRIDGE,DISCONNECTED\n",
            outbox,
            "_mark_disconnected must be called after blocking readline() raises",
        )


# ---------------------------------------------------------------------------
# TestCommandParserBridgeOta — BRIDGE_OTA_MODE forwarding
# ---------------------------------------------------------------------------

class TestCommandParserBridgeOta(unittest.TestCase):
    """CommandParser routing for BRIDGE_OTA_MODE (bridge OTA update flow)."""

    def _make_parser(self, bridge_connected: bool):
        from command_parser import CommandParser
        outbox: list[str] = []
        parser = CommandParser(
            MagicMock(),   # settings
            MagicMock(),   # gpio
            MagicMock(),   # lidar
            MagicMock(),   # trigger_engine
            outbox.append,
        )
        bridge = MagicMock()
        bridge.connected = bridge_connected
        parser.set_bridge(bridge)
        return parser, bridge, outbox

    def test_forwards_to_bridge_when_connected_with_token(self):
        parser, bridge, outbox = self._make_parser(bridge_connected=True)
        parser.handle("BRIDGE_OTA_MODE")
        bridge.send.assert_called_once()
        sent = bridge.send.call_args[0][0]
        prefix, _, token = sent.partition(",")
        self.assertEqual(prefix, "BRIDGE_OTA_MODE")
        # Token must be a 32-char hex secret (secrets.token_hex(16)).
        # It doubles as the OTA AP's WPA2 password, so it must be >= 8 chars
        # (WPA2 minimum) and must never be a fixed/documented value.
        self.assertRegex(token, r"^[0-9a-f]{32}$")
        self.assertGreaterEqual(len(token), 8)
        self.assertNotEqual(token, "lrbridge123")

    def test_token_is_unique_per_request(self):
        parser, bridge, outbox = self._make_parser(bridge_connected=True)
        parser.handle("BRIDGE_OTA_MODE")
        parser.handle("BRIDGE_OTA_MODE")
        tokens = [c.args[0].partition(",")[2] for c in bridge.send.call_args_list]
        self.assertEqual(len(tokens), 2)
        self.assertNotEqual(tokens[0], tokens[1])

    def test_no_local_response_when_bridge_connected(self):
        # Bridge acks asynchronously (BRIDGE_OTA_MODE,OK) — the parser must not
        # emit its own ack, or the app would see a duplicate.
        parser, bridge, outbox = self._make_parser(bridge_connected=True)
        parser.handle("BRIDGE_OTA_MODE")
        self.assertEqual(outbox, [])

    def test_err_no_bridge_when_disconnected(self):
        parser, bridge, outbox = self._make_parser(bridge_connected=False)
        parser.handle("BRIDGE_OTA_MODE")
        self.assertIn("BRIDGE_OTA_MODE,ERR_NO_BRIDGE\n", outbox)
        bridge.send.assert_not_called()

    def test_err_no_bridge_when_no_bridge_object(self):
        from command_parser import CommandParser
        outbox: list[str] = []
        parser = CommandParser(
            MagicMock(), MagicMock(), MagicMock(), MagicMock(), outbox.append
        )
        parser.handle("BRIDGE_OTA_MODE")
        self.assertIn("BRIDGE_OTA_MODE,ERR_NO_BRIDGE\n", outbox)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
