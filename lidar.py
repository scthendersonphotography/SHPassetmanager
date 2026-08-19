"""
TF-Luna LiDAR driver for Raspberry Pi via I²C.

Register map (0x00–0x05):
  0x00  dist_low    }  distance in cm (uint16 LE)
  0x01  dist_high   }
  0x02  flux_low    }  signal strength (uint16 LE)
  0x03  flux_high   }
  0x04  temp_low    }  temp × 100 K (uint16 LE) → °C = raw/100 − 273.15
  0x05  temp_high   }

Default I²C address: 0x10
Typical poll rate: 100 Hz (10 ms sleep between reads)
"""

import threading
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from settings import Settings

TFL_DIST_REG = 0x00  # read 6 bytes from here

try:
    import smbus2  # type: ignore
    _SMBUS_AVAILABLE = True
except ImportError:
    _SMBUS_AVAILABLE = False
    print("[LiDAR] smbus2 not found — distance will read 0 (sensor not available)")


class LiDARDriver:
    def __init__(self, settings: "Settings") -> None:
        self._settings = settings
        self._lock = threading.Lock()
        self._distance: int = 0
        self._flux: int = 0
        self._temp: float = 0.0
        self._running = False
        self._thread: threading.Thread | None = None
        self._bus = None

    # ── Read-only properties (thread-safe) ────────────────────────────────────

    @property
    def distance(self) -> int:
        with self._lock:
            return self._distance

    @property
    def flux(self) -> int:
        with self._lock:
            return self._flux

    @property
    def temp(self) -> float:
        with self._lock:
            return self._temp

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(target=self._poll_loop, daemon=True, name="lidar-poll")
        self._thread.start()
        print("[LiDAR] Poll thread started")

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=2.0)
        if self._bus:
            try:
                self._bus.close()
            except Exception:
                pass
        print("[LiDAR] Stopped")

    # ── Background poll ───────────────────────────────────────────────────────

    def _poll_loop(self) -> None:
        bus_num = self._settings.get("lidarBus", 1)
        addr = self._settings.get("lidarAddr", 0x10)

        if _SMBUS_AVAILABLE:
            try:
                self._bus = smbus2.SMBus(bus_num)
                print(f"[LiDAR] Opened I²C bus {bus_num}, addr=0x{addr:02X}")
            except Exception as e:
                print(f"[LiDAR] Cannot open I²C bus {bus_num}: {e} — sim mode (distance=0)")
                self._bus = None

        consecutive_errors = 0

        while self._running:
            if self._bus is not None:
                try:
                    data = self._bus.read_i2c_block_data(addr, TFL_DIST_REG, 6)
                    dist = (data[1] << 8) | data[0]
                    flux = (data[3] << 8) | data[2]
                    temp_raw = (data[5] << 8) | data[4]
                    temp_c = round(temp_raw / 100.0 - 273.15, 1)
                    with self._lock:
                        self._distance = dist
                        self._flux = flux
                        self._temp = temp_c
                    consecutive_errors = 0
                except Exception as e:
                    consecutive_errors += 1
                    if consecutive_errors == 1 or consecutive_errors % 500 == 0:
                        print(f"[LiDAR] Read error #{consecutive_errors}: {e}")
                    with self._lock:
                        self._distance = 0
            # ~100 Hz
            time.sleep(0.01)
