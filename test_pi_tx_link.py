"""Failure-path tests for the Pi TX half of the ESP-NOW link transaction."""

import unittest
from unittest.mock import MagicMock

from command_parser import CommandParser


class TestPiTxLinkCommandRouting(unittest.TestCase):
    def _make_parser(self, bridge_connected: bool):
        outbox: list[str] = []
        parser = CommandParser(
            MagicMock(), MagicMock(), MagicMock(), MagicMock(), outbox.append
        )
        bridge = MagicMock()
        bridge.connected = bridge_connected
        parser.set_bridge(bridge)
        return parser, bridge, outbox

    def test_pair_returns_err_no_bridge_when_ble_side_already_succeeded(self):
        """The app can roll back its BLE pairing after this explicit failure."""
        parser, bridge, outbox = self._make_parser(bridge_connected=False)
        parser.handle("ESPNOW_PAIR_LXTX,AA:BB:CC:DD:EE:FF")

        self.assertEqual(outbox, ["ESPNOW_PAIR_LXTX,ERR_NO_BRIDGE\n"])
        bridge.send.assert_not_called()

    def test_unlink_does_not_falsely_ack_when_bridge_is_disconnected(self):
        parser, bridge, outbox = self._make_parser(bridge_connected=False)
        parser.handle("ESPNOW_REMOVE_LXTX")

        self.assertEqual(outbox, ["ESPNOW_REMOVE_LXTX,ERR_NO_BRIDGE\n"])
        bridge.send.assert_not_called()

    def test_pair_and_unlink_forward_when_bridge_is_available(self):
        parser, bridge, outbox = self._make_parser(bridge_connected=True)

        parser.handle("ESPNOW_PAIR_LXTX,AA:BB:CC:DD:EE:FF")
        parser.handle("ESPNOW_REMOVE_LXTX")

        self.assertEqual(outbox, [])
        self.assertEqual(
            [call.args[0] for call in bridge.send.call_args_list],
            ["ESPNOW_PAIR_LXTX,AA:BB:CC:DD:EE:FF", "ESPNOW_REMOVE_LXTX"],
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()