#!/usr/bin/env bash
# Pi TX install script
# Run once on the Raspberry Pi after cloning the repo:
#   cd artifacts/pi-tx && sudo ./install.sh
#
# What it does:
#   1. Installs system packages (Python 3.11+, pigpio, avahi-daemon)
#   2. Installs Python dependencies via pip
#   3. Enables I²C and disables serial console (for UART bridge, task #433)
#   4. Installs and enables the pi-tx systemd service
#
# Tested on: Raspberry Pi OS Lite (Bookworm, 64-bit), Pi 3B+ / 4 / 5

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "=== Pi TX installer ==="
echo "Working directory: $SCRIPT_DIR"

# ── 1. System packages ────────────────────────────────────────────────────────
echo ""
echo "→ Installing system packages…"
apt-get update -qq
apt-get install -y --no-install-recommends \
    python3 python3-pip python3-dev \
    i2c-tools \
    avahi-daemon \
    nodejs npm \
    libgphoto2-dev \
    python3-picamera2 libcamera-apps \
    python3-opencv
# libgphoto2-dev      → USB DSLR support (gphoto2)
# python3-picamera2 + libcamera-apps → CSI Pi Camera support (camMode 7)
# python3-opencv      → basic USB webcam support (camMode 8)

# ── 2. Python packages ────────────────────────────────────────────────────────
echo ""
echo "→ Installing Python packages…"
pip3 install --break-system-packages -r "$SCRIPT_DIR/requirements.txt"

# ── 3. Enable I²C (needed for TF-Luna LiDAR) ─────────────────────────────────
echo ""
echo "→ Enabling I²C interface…"
raspi-config nonint do_i2c 0     # 0 = enable

# ── 4. Disable serial console (frees /dev/serial0 for ESP-NOW bridge UART) ───
echo ""
echo "→ Disabling serial login shell (keeps hardware UART free for bridge)…"
raspi-config nonint do_serial_hw 0     # enable hardware UART
raspi-config nonint do_serial_cons 1   # disable login shell over serial

# ── 5. Enable mDNS so pi-tx.local resolves on the LAN ────────────────────────
echo ""
echo "→ Enabling avahi-daemon (mDNS / pi-tx.local)…"
systemctl enable --now avahi-daemon

# Set mDNS hostname to pi-tx so the device is reachable at pi-tx.local
hostnamectl set-hostname pi-tx
echo "  Hostname set to pi-tx  (access via http://pi-tx.local)"

# ── 6. Build the browser control panel (web UI) ──────────────────────────────
echo ""
echo "→ Building web control panel (served at http://pi-tx.local)…"
(
    cd "$SCRIPT_DIR/ui"
    npm ci --no-audit --no-fund
    npm run build
)
echo "  Web UI built → $SCRIPT_DIR/static"

# ── 7. Install systemd service ────────────────────────────────────────────────
echo ""
echo "→ Installing pi-tx systemd service…"

# Determine which user account will own the service.
# SUDO_USER is set by sudo to the original caller; fall back to the current
# effective user if the script is run without sudo (e.g. already root).
SERVICE_USER="${SUDO_USER:-$(id -un)}"
if ! id "$SERVICE_USER" &>/dev/null; then
    echo "ERROR: Detected service user '$SERVICE_USER' does not exist."
    echo "  Run the script with: sudo -u <your-username> ./install.sh"
    exit 1
fi
echo "  Service will run as user: $SERVICE_USER"

# Patch the service file: substitute both __INSTALL_DIR__ and __SERVICE_USER__
SERVICE_SRC="$SCRIPT_DIR/systemd/pi-tx.service"
SERVICE_DST="/etc/systemd/system/pi-tx.service"
sed \
    -e "s|__INSTALL_DIR__|$SCRIPT_DIR|g" \
    -e "s|__SERVICE_USER__|$SERVICE_USER|g" \
    "$SERVICE_SRC" > "$SERVICE_DST"

systemctl daemon-reload
systemctl enable pi-tx
systemctl restart pi-tx

echo ""
echo "=== Installation complete ==="
echo ""
echo "  Service status:  systemctl status pi-tx"
echo "  Live logs:       journalctl -u pi-tx -f"
echo "  Settings file:   ~/.pi-tx/settings.json"
echo ""
echo "  Connect the phone app:"
echo "    WiFi Direct → enter the Pi's IP address, or"
echo "    use pi-tx.local once mDNS resolves on your network."
echo ""
echo "  Hardware wiring (BCM pin numbers):"
echo "    TF-Luna SDA → Pin 3  (GPIO 2)"
echo "    TF-Luna SCL → Pin 5  (GPIO 3)"
echo "    TF-Luna VCC → Pin 4  (5 V)"
echo "    TF-Luna GND → Pin 6  (GND)"
echo "    Shutter trigger → Pin 11 (GPIO 17)"
echo "    Wake / AF pulse → Pin 13 (GPIO 27)"
echo ""
echo "Reboot recommended to apply I²C and serial changes:"
echo "  sudo reboot"
