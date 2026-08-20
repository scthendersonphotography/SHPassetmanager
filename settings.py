"""
Persistent settings for Pi TX daemon.
Mirrors the ESP32 NVS key names and defaults so the phone app's
GET_INFO / S,... / W,... handshakes work unchanged.
"""

import json
import threading
from pathlib import Path
from typing import Any

SETTINGS_PATH = Path.home() / ".pi-tx" / "settings.json"

# Keys and defaults mirror the ESP32 NVS schema exactly
DEFAULTS: dict[str, Any] = {
    # Device identity
    # Keep the protocol identity expected by existing app builds. Pi-specific
    # capabilities are advertised separately with PLATFORM,PI.
    "name": "LASER_EYE_V2",
    # LiDAR trigger settings  (S,… command fields)
    "minDist": 0,
    "maxDist": 800,
    "cooldown": 1000,       # ms
    "shots": 1,
    "pulseDur": 300,        # ms
    "invertTrig": False,
    "delayShot": 0,         # ms
    # Wake pulse  (W,… command)
    "wakeEn": False,
    "wakeIntervalMs": 300000,   # ms = 300 s
    # Output polarity  (TRIG_MODE / WAKE_MODE)
    "trigMode": 0,          # 0 = dry contact, 1 = hot 3.3 V
    "wakeMode": 0,
    # Autofocus pre-pulse  (AF,… command)
    "afEnabled": False,
    "afPreMs": 500,
    # LEDs  (LEDS,… command)
    "statusLedEn": True,
    "powerLedEn": True,
    # Flux gate  (FLUX,… command)
    "minFlux": 20,
    # ESP-NOW (commands forwarded to bridge co-processor, task #433)
    "espNowEn": False,
    # Camera direct mode (USB camera, task #434)
    "camMode": 0,
    # Last-used USB camera settings — re-applied on reconnect so the camera and
    # app dropdowns are in sync without a disconnect/reconnect cycle.
    "usbIso": "",
    "usbAperture": "",
    "usbSs": "",
    # Trigger schedule — UTC minutes (SCHED,… command)
    "schedEn": False,
    "schedStart": 0,        # 0–1439 UTC minutes since midnight
    "schedEnd": 1439,
    # GPIO pin assignments (Pi BCM numbering)
    "triggerPin": 17,
    "wakePin": 27,
    "laserPin": 22,         # alignment laser EN, -1 to disable
    "statusLedPin": -1,     # -1 = not wired
    "powerLedPin": -1,
    # I²C bus for TF-Luna
    "lidarBus": 1,
    "lidarAddr": 0x10,
}


class Settings:
    def __init__(self, path: Path = SETTINGS_PATH) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._data: dict[str, Any] = dict(DEFAULTS)
        self._load()

    # ── Public API ─────────────────────────────────────────────────────────────

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._data.get(key, DEFAULTS.get(key, default))

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = value
        self._save()

    def update(self, updates: dict[str, Any]) -> None:
        with self._lock:
            self._data.update(updates)
        self._save()

    def all(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._data)

    # ── Persistence ────────────────────────────────────────────────────────────

    def _load(self) -> None:
        try:
            if self._path.exists():
                with open(self._path) as f:
                    stored = json.load(f)
                with self._lock:
                    self._data.update(stored)
                print(f"[Settings] Loaded {self._path}")
        except Exception as e:
            print(f"[Settings] Load failed ({e}) — using defaults")

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._lock:
                data = dict(self._data)
            with open(self._path, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            print(f"[Settings] Save failed: {e}")
