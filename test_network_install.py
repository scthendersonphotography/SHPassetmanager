"""Static contract tests for the Pi TX local-first network installation."""

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parent
INSTALLER = (ROOT / "install.sh").read_text(encoding="utf-8")
SERVICE = (ROOT / "systemd" / "pi-tx.service").read_text(encoding="utf-8")
SETTINGS = (ROOT / "settings.py").read_text(encoding="utf-8")


class TestHotspotInstallContract(unittest.TestCase):
    def test_hotspot_is_persistent_and_uses_the_fixed_local_address(self):
        self.assertIn("autoconnect=true", INSTALLER)
        self.assertIn("mode=ap", INSTALLER)
        self.assertIn("method=shared", INSTALLER)
        self.assertIn("addresses=10.42.0.1/24", INSTALLER)

    def test_hotspot_credentials_are_generated_and_root_only(self):
        self.assertIn("secrets.token_urlsafe", INSTALLER)
        self.assertIn('chmod 600 "$HOTSPOT_ENV"', INSTALLER)
        self.assertIn('chmod 600 "$NM_PROFILE"', INSTALLER)

    def test_service_environment_does_not_receive_the_hotspot_password(self):
        self.assertIn("EnvironmentFile=-/etc/pi-tx-network.env", SERVICE)
        self.assertNotIn("pi-tx-hotspot.env", SERVICE)

    def test_service_waits_for_network_manager_on_boot(self):
        self.assertIn("After=NetworkManager.service network-online.target", SERVICE)
        self.assertIn("Wants=network-online.target", SERVICE)
        self.assertIn("Restart=always", SERVICE)

    def test_protocol_name_remains_compatible_with_existing_app_builds(self):
        self.assertIn('"name": "LASER_EYE_V2"', SETTINGS)


if __name__ == "__main__":
    unittest.main()