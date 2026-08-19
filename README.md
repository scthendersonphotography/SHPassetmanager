# Pi TX — Raspberry Pi Camera Trigger Daemon

A Python service that turns any Raspberry Pi 3+ into a Laser Eye TX-compatible
camera trigger. The phone app connects to it over WiFi exactly the same way it
connects to the ESP32 Laser Eye TX — no app changes required.

## Features

| Feature | Status |
|---|---|
| GET /data + POST /cmd HTTP API (same as ESP32 TX) | ✅ |
| TF-Luna LiDAR via I²C (distance, flux, temp) | ✅ |
| GPIO trigger output (dry-contact + hot modes) | ✅ |
| GPIO wake/AF pre-pulse output | ✅ |
| All core commands (S, AF, W, FLUX, LEDS, LASER, SCHED, …) | ✅ |
| Settings persistence (JSON) | ✅ |
| mDNS / `pi-tx.local` auto-discovery | ✅ |
| systemd auto-start + watchdog | ✅ |
| ESP-NOW bridge (wireless RX modules) | ✅ Implemented; requires the separate ESP32 bridge hardware and firmware |
| USB camera control (gPhoto2) | ✅ Implemented; requires a supported USB camera and gPhoto2 |
| Google Drive image upload | ✅ Implemented; requires Google Drive API OAuth credentials |
| Time-lapse + astronomical scheduling | ✅ Implemented; astronomical scheduling requires observer coordinates |

## Browser control panel (web UI)

The Pi serves a full control panel to any browser on the same network:

- Open **http://pi-tx.local** (mDNS via avahi, set up by `install.sh`) or `http://<pi-ip>`
- Live telemetry (distance / flux / temp), trigger & wake controls, ESP-NOW
  receiver pairing, USB camera settings, Google Drive uploads, time-lapse,
  and astronomical triggers — the same features as the phone app.
- Source lives in `ui/` (React + Vite). `install.sh` runs `npm install &&
  npm run build`, which emits static files into `static/`; FastAPI serves
  them at `/ui` and redirects `/` there. No CDN or internet access needed.
- To rebuild manually: `cd ui && npm install && npm run build`, then
  `sudo systemctl restart pi-tx`.
- The web UI uses the same `GET /data` / `POST /cmd` protocol as the phone
  app, plus a small `GET /telemetry` JSON endpoint for flux/temp.

## Hardware wiring

All pins use BCM numbering. Defaults are configurable in `~/.pi-tx/settings.json`.

### TF-Luna LiDAR (I²C, address 0x10)

| TF-Luna wire | Pi physical pin | Pi GPIO |
|---|---|---|
| VCC (red) | Pin 4 | 5 V |
| GND (black) | Pin 6 | GND |
| SDA (green) | Pin 3 | GPIO 2 |
| SCL (blue) | Pin 5 | GPIO 3 |

Enable I²C in raspi-config: **Interface Options → I2C → Enable**
(the install script does this automatically).

### Trigger + wake outputs

| Signal | Pi physical pin | Pi GPIO |
|---|---|---|
| Shutter trigger | Pin 11 | GPIO 17 |
| Wake / AF pre-pulse | Pin 13 | GPIO 27 |

**Dry-contact mode** (trigMode=0): idle = floating (high-Z).

Two safe wiring options:

*Option A — direct sink (no isolation):* Connect GPIO pin directly to the
camera's shutter tip, GND to camera GND. Active level = LOW (GPIO pulls
shutter line to GND). Simple but provides no electrical isolation between
Pi and camera.

*Option B — isolated (recommended):* Use an NPN transistor (2N3904) or
optocoupler (PC817). Wire the GPIO → base/LED (via 1 kΩ); camera shutter
tip → collector/detector; GND → emitter/detector GND. In this circuit
**GPIO HIGH turns the transistor/optocoupler ON**, which sinks the camera
line. Set `invertTrig=1` (via the `S` command or settings JSON) so the Pi
TX drives the GPIO HIGH when triggering rather than LOW. Enables full
isolation between the Pi's 3.3 V logic and the camera shutter cable.

**Hot mode** (trigMode=1): 3.3 V direct to camera shutter tip. For cameras
that accept a 3.3 V logic signal on the shutter jack.

**invertTrig** (`S` command field 6, or `"invertTrig": 1` in settings):
Inverts the active GPIO level. Use with dry-contact + transistor/optocoupler
(Option B above). Default: 0 (active LOW — Option A wiring).

### ESP-NOW bridge co-processor (UART — no USB port consumed)

The bridge is a bare ESP32 module running `firmware/pi-espnow-bridge/pi_espnow_bridge.ino`.
It connects to the Pi via hardware UART (GPIO 14/15) so all four Pi USB-A ports stay free
for cameras, storage, and peripherals.

**Wiring (4 wires — no level shifter, both sides are 3.3 V logic)**

| Signal | Pi physical pin | Pi GPIO | ESP32 pin |
|---|---|---|---|
| Pi TX → Bridge RX | Pin 8 | GPIO 14 | UART2 RX (GPIO 16) |
| Pi RX ← Bridge TX | Pin 10 | GPIO 15 | UART2 TX (GPIO 17) |
| GND | Pin 14 | — | GND |
| 3.3 V power | Pin 17 | — | 3.3 V (bare module only; dev-kit = USB) |

**raspi-config serial setup (one-time)**

The Pi serial console must be disabled so `/dev/serial0` is free for the bridge.
The install script handles this automatically with:

```bash
raspi-config nonint do_serial_cons 1   # disable login shell on serial
raspi-config nonint do_serial_hw 0     # keep hardware UART enabled
```

To do it manually: **raspi-config → Interface Options → Serial Port →
"Would you like a login shell…?" → No → "Would you like the serial port hardware enabled?" → Yes**

Then reboot.

**Flashing the bridge firmware (USB, one-time)**

1. Install the Arduino IDE and add the ESP32 board package
   (Boards Manager URL: `https://raw.githubusercontent.com/espressif/arduino-esp32/gh-pages/package_esp32_index.json`)
2. Install the OTA server libraries (Sketch → Include Library → Manage Libraries,
   or Add .ZIP Library from the GitHub releases):
   - **ESPAsyncWebServer** (me-no-dev / ESP32Async)
   - **AsyncTCP** (dependency of ESPAsyncWebServer)

   These are the same libraries the Mini RX V2 firmware uses for its OTA mode.
3. Open `firmware/pi-espnow-bridge/pi_espnow_bridge.ino`
4. Select board: **ESP32 Dev Module** (or ESP32-C3-Mini / ESP32-S3-Mini with GPIO remap)
5. Select the USB port for your bridge ESP32
6. Click **Upload**
7. Disconnect USB from bridge, then wire the 4-pin UART connector to the Pi
8. Restart the Pi TX service: `sudo systemctl restart pi-tx`

**Updating the bridge firmware later (OTA, no USB cable)**

Once an OTA-capable build is installed, later updates can be applied from the
phone app: LiDAR tab → **BRIDGE FIRMWARE** (shown while connected to a Pi TX).
The app sends `BRIDGE_OTA_MODE` through the Pi daemon, which generates a fresh
random session token and forwards `BRIDGE_OTA_MODE,<token>` to the bridge; the
bridge acks with `BRIDGE_OTA_MODE,OK,<token>`, tears down ESP-NOW and raises a
temporary WiFi AP
(**LR-BRIDGE-OTA**, LED fast-blinks) whose WPA2 password **is** that
per-session token — there is no fixed or published AP credential, so nearby
parties cannot join the OTA network or observe upload traffic. The app shows
the session password in its join instructions. After joining, the app uploads
the new `.bin` to `http://192.168.4.1/update?token=<token>`; uploads without
the current session token are rejected with 403, and the bridge additionally
rejects files that are not a valid ESP32 firmware image (0xE9 magic byte).
The bridge reboots after flashing and re-announces `ESPNOW_BRIDGE_READY` to
the Pi.
If no bridge is connected, the daemon answers `BRIDGE_OTA_MODE,ERR_NO_BRIDGE`.
To abort while in OTA mode, power-cycle the bridge — it boots back to normal
operation.

**Publishing bridge firmware for OTA**

The app looks for the bridge `.bin` in the latest release of the firmware
GitHub repository (same place as the Cable Trigger / Laser Eye binaries).
Naming contract: the asset filename must contain "espnow bridge" (dots,
underscores or hyphens are treated as spaces) and end in `.bin`, with a
semantic version in the name — e.g. `ESPNOW.Bridge.firmware.v1.0.0.bin` or
`pi-espnow-bridge-1.0.0.bin`. Export the compiled binary from Arduino IDE
(Sketch → Export Compiled Binary) and attach it to the release.

The Pi daemon opens `/dev/serial0` automatically on startup and retries every 5 s if the
bridge is absent or power-cycled. The app shows the bridge status in the ESP-NOW section.

**What the bridge does**

- Stores the RX peer list in its own NVS (survives Pi reboots)
- Translates `ESPNOW_PAIR`, `ESPNOW_REMOVE`, etc. from the Pi daemon into ESP-NOW peer management
- Fan-outs `TX_TRIGGER`, `TX_BURST`, `TX_WAKE` to all paired RX modules on each shot
- Forwards incoming `TXBATT`, `ESPNOW_RX_INFO`, `ESPNOW_RX_FW` packets from RX modules back to the Pi
- Announces `ESPNOW_BRIDGE_READY` on boot; Pi daemon responds with a state sync

**Supported bridge boards**

| Board | Notes |
|---|---|
| ESP32-DevKit-C (30 or 38 pin) | Primary target; plug-in UART2 GPIO 16/17 |
| ESP32-DevKit-WROOM-32 | Pin-compatible with DevKit-C |
| ESP32-C3-Mini | Change `BRIDGE_UART_RX`/`TX` defines to safe GPIO (e.g. 6/7) |
| ESP32-S3-Mini | Change `BRIDGE_UART_RX`/`TX` defines (e.g. 4/5) |

## Google Drive Setup

The Google Drive upload feature requires an OAuth 2.0 client ID and secret from
Google Cloud. Once set, the Pi handles authorization entirely — the app guides
you through a device-flow code on first use.

### 1 · Create Google Cloud credentials

1. Go to [console.cloud.google.com](https://console.cloud.google.com).
2. Create a new project (or use an existing one).
3. Enable **Google Drive API**: *APIs & Services → Library → Google Drive API → Enable*.
4. Create credentials: *APIs & Services → Credentials → Create Credentials →
   OAuth client ID*.
   - Application type: **TV and Limited Input devices** (enables the device authorization
     grant / `urn:ietf:params:oauth:grant-type:device_code` flow the Pi uses).
5. Download the JSON or copy the **Client ID** and **Client secret**.

### 2 · Configure the Pi

**Option A — `/etc/pi-tx.conf` (recommended)**

```bash
sudo nano /etc/pi-tx.conf
```

Add:

```ini
GDRIVE_CLIENT_ID=<your-client-id>.apps.googleusercontent.com
GDRIVE_CLIENT_SECRET=<your-client-secret>
```

Save, then restart the service:

```bash
sudo systemctl restart pi-tx
```

The systemd unit reads `/etc/pi-tx.conf` via `EnvironmentFile=-/etc/pi-tx.conf`
(the `-` prefix means the file is optional — the service still starts if absent).

**Option B — credentials JSON file**

Place the downloaded JSON at `~/.pi-tx/gdrive_client_secrets.json`.
The daemon loads it automatically on start. The file must be owned and readable
only by the service user:

```bash
chmod 600 ~/.pi-tx/gdrive_client_secrets.json
```

### 3 · Authorize from the app

1. Connect the phone app to the Pi TX.
2. Open the **GOOGLE DRIVE** section in the Lidar tab.
3. Tap **AUTHORIZE WITH GOOGLE** — the Pi emits a URL and a short code.
4. Open the URL on any browser, sign in, and enter the code.
5. Tap **ALREADY APPROVED? CHECK STATUS** — the Pi confirms with your email.

The token is saved to `~/.pi-tx/gdrive_token.json` (mode 0600) and
auto-refreshed; you only need to authorize once.

## Installation

### Get the Pi TX files

The Pi TX source is in the `pi-tx/` directory of the SHP Asset Manager
repository:

<https://github.com/scthendersonphotography/SHPassetmanager/tree/main>

If the Pi TX changes are still on the release branch, clone that branch
instead:

```bash
git clone --branch scthendersonphotography-release/pi-tx-initial \
  https://github.com/scthendersonphotography/SHPassetmanager.git
cd SHPassetmanager
```

After the branch is merged into `main`, use:

```bash
git clone https://github.com/scthendersonphotography/SHPassetmanager.git
cd SHPassetmanager
```

If you received the standalone `pi-tx.zip` package, extract it and enter the
directory that contains `install.sh`:

```bash
unzip pi-tx.zip
cd pi-tx
```

Do not use a nested `pi-tx/pi-tx` directory. `install.sh`, `main.py`, and
`requirements.txt` must be in the directory from which you run the installer.

```bash
chmod +x install.sh
sudo ./install.sh
sudo reboot
```

After reboot the service starts automatically. Check with:

```bash
systemctl status pi-tx
journalctl -u pi-tx -f
```

## Connecting the phone app

1. Ensure the Pi and phone are on the same WiFi network (home router,
   phone hotspot, or dedicated field router).
2. In the Laser Eye app: tap the WiFi icon → enter `pi-tx.local`
   (or the Pi's IP address if mDNS is unavailable on your network).
3. The app connects identically to an ESP32 TX. Pi-specific UI sections
   appear automatically once the app detects `PLATFORM,PI`.

## Settings file

Settings are stored in `~/.pi-tx/settings.json` and persist across restarts.
Edit manually to change default pin assignments:

```json
{
  "triggerPin": 17,
  "wakePin": 27,
  "laserPin": 22,
  "lidarBus": 1,
  "lidarAddr": 16
}
```

Restart the service after editing: `sudo systemctl restart pi-tx`

## OS dependencies

| Package | Purpose |
|---|---|
| `python3`, `python3-pip` | Runtime |
| `smbus2` (pip) | I²C TF-Luna reads |
| `RPi.GPIO` (pip) | GPIO trigger/wake output |
| `fastapi`, `uvicorn` (pip) | HTTP server |
| `i2c-tools` | I²C bus inspection (`i2cdetect`) |
| `avahi-daemon` | mDNS / `pi-tx.local` |
| `libgphoto2-dev` | USB camera (task #434, installed now) |

## Running without a Pi (development / CI)

The daemon starts cleanly without any Pi hardware. `RPi.GPIO` and `smbus2`
are optional — if not available, GPIO pulses are simulated (logged to stdout)
and LiDAR distance reads return 0. Run with:

```bash
pip install fastapi uvicorn
python main.py
```
