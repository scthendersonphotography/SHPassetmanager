"""
Pi TX daemon — HTTP command server for Raspberry Pi 3+.

Implements the same GET /data + POST /cmd protocol as the ESP32 Laser Eye TX
so the phone app can connect via WiFi without any app changes.

GET  /data   Returns: "<distance_cm>\n<queued_response_lines>"
POST /cmd    Body: raw command string. Returns previously queued responses.

Run directly: python main.py
As a service:  systemctl start pi-tx  (after ./install.sh)
"""

import os
import threading
from collections import deque
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from settings import Settings
from lidar import LiDARDriver
from gpio_control import GPIOControl
from trigger_engine import TriggerEngine
from command_parser import CommandParser
from espnow_bridge import EspNowBridge
from camera_usb import CameraUSB
from camera_pi import CameraPi
from camera_webcam import CameraWebcam
from camera_download import CameraDownloader
from gdrive_upload import GDriveUploader
from timelapse import TimeLapse
from astro_scheduler import AstroScheduler

# ── Thread-safe outbox ────────────────────────────────────────────────────────
# Multiple threads (trigger engine, wake timer, command parser) write here;
# HTTP handlers drain it. Matches the ESP32's httpOutbox + mutex pattern.

_outbox: deque[str] = deque()
_outbox_lock = threading.Lock()


def send_response(line: str) -> None:
    """Enqueue a response line for the next GET /data or POST /cmd poll."""
    if not line.endswith("\n"):
        line += "\n"
    with _outbox_lock:
        _outbox.append(line)


def drain_outbox() -> str:
    """Drain and return all queued response lines as a single string."""
    with _outbox_lock:
        if not _outbox:
            return ""
        result = "".join(_outbox)
        _outbox.clear()
        return result


# ── Singletons ────────────────────────────────────────────────────────────────
settings       = Settings()
gpio           = GPIOControl(settings)
lidar          = LiDARDriver(settings)
trigger_engine = TriggerEngine(settings, gpio, lidar, send_response)
cmd_parser     = CommandParser(settings, gpio, lidar, trigger_engine, send_response)

# ESP-NOW bridge — connects asynchronously in the background.
# Both cmd_parser and trigger_engine get a reference so they can forward
# ESP-NOW commands / trigger fan-out through the bridge.
bridge = EspNowBridge(send_response)
cmd_parser.set_bridge(bridge)
trigger_engine.set_bridge(bridge)
# Route Laser Eye TX ESP-NOW triggers (via the bridge) into the trigger engine.
bridge.set_lx_trigger_callback(lambda ms: trigger_engine.fire_espnow_lx(ms))

# USB camera — hot-plug thread runs in background; connect() called explicitly
# when the app sends CAMERA_DIRECT,6. No-op when python-gphoto2 not installed.
camera = CameraUSB(send_response, settings=settings)
cmd_parser.set_camera(camera)
trigger_engine.set_camera(camera)

# CSI Pi Camera (camMode 7) and basic USB webcam (camMode 8) — direct-capture
# cameras that write JPEGs to the shared staging dir. No-ops when picamera2 /
# cv2 are not installed (e.g. developing on non-Pi hardware).
camera_pi = CameraPi(send_response, settings=settings)
cmd_parser.set_camera_pi(camera_pi)
trigger_engine.set_camera_pi(camera_pi)

camera_webcam = CameraWebcam(send_response, settings=settings)
cmd_parser.set_camera_webcam(camera_webcam)
trigger_engine.set_camera_webcam(camera_webcam)

# Google Drive uploader + camera image downloader.
# No-op when google-api-python-client / python-gphoto2 are not installed.
gdrive     = GDriveUploader(send_response)
downloader = CameraDownloader()
cmd_parser.set_gdrive(gdrive)
cmd_parser.set_downloader(downloader)

# Time-lapse engine — fires trigger_engine.fire_manual() at a set interval.
timelapse = TimeLapse(trigger_engine=trigger_engine, send_response=send_response)
cmd_parser.set_timelapse(timelapse)

# Astronomical scheduler — schedules time-lapses at sunrise/sunset/moon events.
# Location is restored from settings if previously set.
_astro_lat = settings.get("astroLat", None)
_astro_lon = settings.get("astroLon", None)
astro_scheduler = AstroScheduler(
    timelapse=timelapse,
    send_response=send_response,
    lat=_astro_lat,
    lon=_astro_lon,
)
cmd_parser.set_astro_scheduler(astro_scheduler)


# ── App lifespan ──────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    lidar.start()
    trigger_engine.start()
    bridge.start()     # opens /dev/serial0 in background — no-op if absent
    camera.start()         # starts USB hot-plug thread — no-op if gphoto2 absent
    camera_pi.start()      # no-op (CSI cameras probed on connect)
    camera_webcam.start()  # starts webcam hot-plug thread — no-op if cv2 absent
    # Protocol compatibility: existing app builds validate this INFO name.
    # Pi-specific behavior is identified by PLATFORM,PI / X-Platform instead.
    name = settings.get("name", "LASER_EYE_V2")
    port = int(os.environ.get("PORT", 80))
    print(f"[Pi TX] {name} ready on port {port}")
    yield
    print("[Pi TX] Shutting down …")
    camera_webcam.stop()
    camera_pi.stop()
    camera.stop()
    bridge.stop()
    trigger_engine.stop()
    lidar.stop()
    gpio.cleanup()


app = FastAPI(lifespan=lifespan)


# ── HTTP endpoints ────────────────────────────────────────────────────────────

@app.get("/data")
async def get_data() -> Response:
    """
    Returns current LiDAR distance (first line) followed by any queued
    firmware responses. Matches the ESP32's HTTP /data endpoint exactly.
    """
    dist = lidar.distance
    body = f"{dist}\n"
    queued = drain_outbox()
    if queued:
        body += queued
    return Response(
        content=body,
        media_type="text/plain",
        headers={"X-Platform": "PI"},
    )


@app.post("/cmd")
async def post_cmd(request: Request) -> Response:
    """
    Accepts a raw command string in the POST body.
    Returns previously queued outbox content (or "OK\\n" if empty),
    then processes the command — response appears on the next GET /data poll.
    This matches the ESP32's two-phase behaviour: drain first, then enqueue.
    """
    # Drain BEFORE processing — response to THIS command appears on next poll
    queued = drain_outbox()

    body_bytes = await request.body()
    cmd = body_bytes.decode("utf-8", errors="replace").strip()
    if cmd:
        cmd_parser.handle(cmd)

    return Response(
        content=queued if queued else "OK\n",
        media_type="text/plain",
    )


@app.get("/preview.jpg")
async def get_preview() -> Response:
    """
    Single JPEG frame from the active Pi Camera's always-on lores stream.
    ~640×360, quality 70 — suitable for a 1–2 fps focus/exposure preview.
    Returns 503 when the Pi Camera is not active or is busy with a capture.
    """
    frame = camera_pi.get_preview_jpeg()
    if frame is None:
        return Response(status_code=503)
    return Response(
        content=frame,
        media_type="image/jpeg",
        headers={"Cache-Control": "no-store, no-cache"},
    )


@app.get("/telemetry")
async def get_telemetry() -> JSONResponse:
    """
    JSON telemetry for the browser control panel (web UI only — the phone app
    keeps using the plain-text /data protocol). Exposes flux and temp, which
    the /data first line does not carry.
    """
    return JSONResponse(
        {"distance": lidar.distance, "flux": lidar.flux, "temp": lidar.temp}
    )


def _upstream_interface() -> str | None:
    """Return the current default-route interface, excluding the Pi hotspot."""
    hotspot_iface = os.environ.get("PI_TX_WIFI_INTERFACE", "wlan0")
    try:
        with open("/proc/net/route", "r", encoding="utf-8") as route_file:
            for line in route_file.readlines()[1:]:
                fields = line.split()
                if len(fields) < 4:
                    continue
                iface, destination, _, flags = fields[:4]
                if (
                    destination == "00000000"
                    and int(flags, 16) & 0x2
                    and iface != hotspot_iface
                ):
                    return iface
    except (OSError, ValueError):
        pass
    return None


@app.get("/network")
async def get_network() -> JSONResponse:
    """Describe local control and optional upstream connectivity."""
    upstream = _upstream_interface()
    return JSONResponse(
        {
            "platform": "PI",
            "localControl": True,
            "hotspotSsid": os.environ.get("PI_TX_HOTSPOT_SSID", "PI_TX"),
            "hotspotAddress": os.environ.get("PI_TX_HOTSPOT_IP", "10.42.0.1"),
            "upstreamAvailable": upstream is not None,
            "upstreamInterface": upstream,
        },
        headers={"Cache-Control": "no-store"},
    )


# ── Web UI (browser control panel) ───────────────────────────────────────────
# Built by `npm run build` in ui/ (see install.sh); output lands in static/.
# Mounted at /ui with an index redirect so http://pi-tx.local/ loads the panel.

_STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

if os.path.isdir(_STATIC_DIR):
    app.mount("/ui", StaticFiles(directory=_STATIC_DIR, html=True), name="ui")

    @app.get("/")
    async def index() -> RedirectResponse:
        return RedirectResponse(url="/ui/")
else:
    print(f"[Pi TX] Web UI not built (missing {_STATIC_DIR}) — run install.sh "
          "or `npm run build` in ui/ to enable the browser control panel")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 80))
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=port,
        log_level="info",
        reload=False,
    )
