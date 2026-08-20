#!/usr/bin/env bash
# Pi TX install script
# Run once on the Raspberry Pi from the directory containing this file:
#   sudo ./install.sh
#
# What it does:
#   1. Installs system packages (Python 3.11+, NetworkManager, avahi-daemon)
#   2. Installs Python dependencies via pip
#   3. Enables I²C and disables the serial console for the UART bridge
#   4. Creates the always-available PI_TX local WiFi network
#   5. Installs and enables the pi-tx systemd service
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
    network-manager \
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

# ── 6. Configure the always-available local-control hotspot ─────────────────
echo ""
echo "→ Configuring the Pi TX local WiFi network…"

HOTSPOT_ENV="/etc/pi-tx-hotspot.env"
HOTSPOT_RUNTIME_ENV="/etc/pi-tx-network.env"
HOTSPOT_IFACE="${PI_TX_WIFI_INTERFACE:-wlan0}"

if [[ ! -d "/sys/class/net/$HOTSPOT_IFACE/wireless" ]]; then
    HOTSPOT_IFACE=""
    for iface_path in /sys/class/net/*/wireless; do
        if [[ -d "$iface_path" ]]; then
            HOTSPOT_IFACE="$(basename "$(dirname "$iface_path")")"
            break
        fi
    done
fi

if [[ -z "$HOTSPOT_IFACE" ]]; then
    echo "ERROR: No WiFi interface was found. Pi TX local control requires WiFi."
    echo "  Set PI_TX_WIFI_INTERFACE before running the installer if the interface is not wlan0."
    exit 1
fi

# Command-line environment values take priority; otherwise preserve credentials
# from a previous install so rerunning the installer never locks out saved phones.
REQUESTED_SSID="${PI_TX_HOTSPOT_SSID:-}"
REQUESTED_PASSWORD="${PI_TX_HOTSPOT_PASSWORD:-}"
if [[ -r "$HOTSPOT_ENV" ]]; then
    # shellcheck disable=SC1090
    source "$HOTSPOT_ENV"
fi

if [[ -n "$REQUESTED_SSID" ]]; then
    PI_TX_HOTSPOT_SSID="$REQUESTED_SSID"
fi
if [[ -n "$REQUESTED_PASSWORD" ]]; then
    PI_TX_HOTSPOT_PASSWORD="$REQUESTED_PASSWORD"
fi

if [[ -z "${PI_TX_HOTSPOT_SSID:-}" ]]; then
    PI_SERIAL="$(tr -d '\0' </proc/device-tree/serial-number 2>/dev/null || true)"
    PI_SUFFIX="${PI_SERIAL: -4}"
    [[ -n "$PI_SUFFIX" ]] || PI_SUFFIX="$(hostname | tr -cd 'A-Za-z0-9' | tail -c 4)"
    [[ -n "$PI_SUFFIX" ]] || PI_SUFFIX="LOCAL"
    PI_TX_HOTSPOT_SSID="PI_TX_${PI_SUFFIX^^}"
fi

if [[ -z "${PI_TX_HOTSPOT_PASSWORD:-}" ]]; then
    PI_TX_HOTSPOT_PASSWORD="$(python3 - <<'PY'
import secrets
print(secrets.token_urlsafe(12).replace("-", "A").replace("_", "B"))
PY
)"
fi

if [[ ! "$PI_TX_HOTSPOT_SSID" =~ ^[A-Za-z0-9._-]{1,32}$ ]]; then
    echo "ERROR: PI_TX_HOTSPOT_SSID must be 1-32 letters, numbers, dots, underscores, or hyphens."
    exit 1
fi
if [[ ! "$PI_TX_HOTSPOT_PASSWORD" =~ ^[A-Za-z0-9._-]{8,63}$ ]]; then
    echo "ERROR: PI_TX_HOTSPOT_PASSWORD must be 8-63 letters, numbers, dots, underscores, or hyphens."
    exit 1
fi

cat > "$HOTSPOT_ENV" <<EOF
PI_TX_HOTSPOT_SSID=$PI_TX_HOTSPOT_SSID
PI_TX_HOTSPOT_PASSWORD=$PI_TX_HOTSPOT_PASSWORD
PI_TX_HOTSPOT_IP=10.42.0.1
PI_TX_WIFI_INTERFACE=$HOTSPOT_IFACE
EOF
chmod 600 "$HOTSPOT_ENV"

# The daemon only needs non-secret network metadata. Do not expose the WPA
# password in the service process environment.
cat > "$HOTSPOT_RUNTIME_ENV" <<EOF
PI_TX_HOTSPOT_SSID=$PI_TX_HOTSPOT_SSID
PI_TX_HOTSPOT_IP=10.42.0.1
PI_TX_WIFI_INTERFACE=$HOTSPOT_IFACE
EOF
chmod 644 "$HOTSPOT_RUNTIME_ENV"

NM_PROFILE="/etc/NetworkManager/system-connections/pi-tx-hotspot.nmconnection"
NM_UUID="$(sed -n 's/^uuid=//p' "$NM_PROFILE" 2>/dev/null | head -n 1)"
[[ -n "$NM_UUID" ]] || NM_UUID="$(cat /proc/sys/kernel/random/uuid)"

cat > "$NM_PROFILE" <<EOF
[connection]
id=pi-tx-hotspot
uuid=$NM_UUID
type=wifi
interface-name=$HOTSPOT_IFACE
autoconnect=true
autoconnect-priority=100

[wifi]
mode=ap
ssid=$PI_TX_HOTSPOT_SSID
band=bg
channel=6

[wifi-security]
key-mgmt=wpa-psk
psk=$PI_TX_HOTSPOT_PASSWORD

[ipv4]
method=shared
addresses=10.42.0.1/24
never-default=true

[ipv6]
method=disabled
EOF
chmod 600 "$NM_PROFILE"

# Raspberry Pi OS Bookworm uses NetworkManager. Avoid a second DHCP manager
# claiming wlan0 on reboot, but do not stop the current service mid-install.
systemctl enable NetworkManager
systemctl disable dhcpcd 2>/dev/null || true
if systemctl is-active --quiet NetworkManager; then
    nmcli connection reload
fi

echo "  Local network: $PI_TX_HOTSPOT_SSID"
echo "  Password:      $PI_TX_HOTSPOT_PASSWORD"
echo "  Pi address:    10.42.0.1"
echo "  Save these credentials — they are also stored root-only in $HOTSPOT_ENV"
echo "  Optional internet can be supplied through Ethernet or a second WiFi adapter."

# ── 7. Build the browser control panel (web UI) ──────────────────────────────
echo ""
echo "→ Building web control panel (served at http://pi-tx.local)…"
(
    cd "$SCRIPT_DIR/ui"
    npm ci --no-audit --no-fund
    npm run build
)
echo "  Web UI built → $SCRIPT_DIR/static"

# ── 8. Install systemd service ────────────────────────────────────────────────
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
echo "    1. Join $PI_TX_HOTSPOT_SSID in phone WiFi settings."
echo "    2. Open the Pi TX tab and connect to 10.42.0.1."
echo "    Local camera control does not require internet."
echo "    On an upstream LAN, pi-tx.local remains available through mDNS."
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
