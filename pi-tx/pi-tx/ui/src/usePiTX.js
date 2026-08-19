/**
 * usePiTX — data transport hook for the Pi TX browser control panel.
 *
 * Mirrors the phone app's WiFiTransport + LiDARContext parsing:
 *   - polls GET /data every 200 ms  → first line = distance, rest = response lines
 *   - polls GET /telemetry every 500 ms → JSON {distance, flux, temp}
 *   - sendCmd(str) POSTs to /cmd and parses any queued lines in the reply
 *
 * All response-line formats match command_parser.py / the ESP32 firmware.
 */
import { useEffect, useReducer, useRef, useCallback } from "react";

const POLL_MS = 200;
const TELEM_MS = 500;
const MAX_HISTORY = 60;
const MAX_LOG = 200;

export const ASTRO_EVENTS = [
  "SUNRISE", "SUNSET", "MOONRISE", "MOONSET",
  "GOLDEN_HOUR_START", "GOLDEN_HOUR_END", "BLUE_HOUR_START", "BLUE_HOUR_END",
];

const MAC_RE = /^([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}),(.+)$/;

export const initialState = {
  connected: false,
  platform: null,
  distance: 0,
  flux: 0,
  temp: 0,
  history: [],
  info: null,          // parsed INFO line
  settings: null,      // parsed S line
  espNowEn: false,
  espMac: null,
  peers: [],           // {mac, type, fw, batt}
  peersFetched: false,
  laserOn: false,
  camMode: 0,          // 0 = off, 6 = USB DSLR, 7 = Pi Camera, 8 = webcam
  camera: {
    status: "off",     // off | connecting | connected | not_found
    model: null, batt: null, remaining: null,
    choices: { iso: [], aperture: [], ss: [] },
    current: { iso: null, aperture: null, ss: null },
    shutterResult: null,
  },
  piCam:  {
    status: "off", model: null, resolution: null,
    // Pi Camera 3 (IMX708) only:
    hasAf: false, afMode: null, afState: null,   // afState: focusing | locked | failed
    lensPos: null, exposure: null, gain: null, awb: null,
  },   // mode 7
  webcam: { status: "off", model: null, resolution: null },   // mode 8
  gdrive: {
    state: null, folder: "", total: 0,
    authUrl: null, authCode: null, email: null,
    progress: null, lastDone: null, error: null,
  },
  timelapse: { state: "idle", shots: 0, remainingSec: 0 },
  astro: {
    lat: null, lon: null,
    preview: [],       // {event, time, date}
    schedules: [],     // {id, event, offsetMin, durationSec, intervalSec}
    error: null,
  },
  log: [],
};

function parseLine(state, line) {
  const clean = line.trim();
  if (!clean) return state;
  const s = { ...state, log: [...state.log.slice(-(MAX_LOG - 1)), `${new Date().toLocaleTimeString()}  ${clean}`] };

  // ── Identity / core settings ────────────────────────────────────────────
  if (clean.startsWith("INFO,")) {
    const p = clean.slice(5).split(",");
    s.info = {
      name: p[0], fw: p[1],
      wakeEn: p[2] === "1", wakeSec: +p[3] || 0,
      statusLedEn: p[4] === "1", powerLedEn: p[5] === "1",
      minFlux: +p[6] || 0, espNowEn: p[7] === "1",
      trigMode: +p[8] || 0, wakeMode: +p[9] || 0,
      delayShot: +p[10] || 0, afEnabled: p[11] === "1", afPreMs: +p[12] || 0,
      batt: +p[13] || 0,
      schedEn: p[14] === "1", schedStart: +p[15] || 0, schedEnd: +p[16] || 0,
    };
    s.espNowEn = s.info.espNowEn;
    return s;
  }
  if (clean.startsWith("PLATFORM,")) { s.platform = clean.slice(9); return s; }
  if (clean.startsWith("S,")) {
    const p = clean.slice(2).split(",");
    s.settings = {
      minDist: +p[0] || 0, maxDist: +p[1] || 0, cooldown: +p[2] || 0,
      shots: +p[3] || 1, pulseDur: +p[4] || 100,
      invertTrig: p[5] === "1", delayShot: +p[6] || 0,
    };
    return s;
  }
  if (clean.startsWith("TRIG_MODE,")) { if (s.info) s.info = { ...s.info, trigMode: +clean.slice(10) || 0 }; return s; }
  if (clean.startsWith("WAKE_MODE,")) { if (s.info) s.info = { ...s.info, wakeMode: +clean.slice(10) || 0 }; return s; }
  if (clean.startsWith("AF,")) {
    const p = clean.slice(3).split(",");
    if (s.info) s.info = { ...s.info, afEnabled: p[0] === "1", afPreMs: +p[1] || 0 };
    return s;
  }
  if (clean.startsWith("WAKE,")) {
    const p = clean.slice(5).split(",");
    if (s.info) s.info = { ...s.info, wakeEn: p[0] === "1", wakeSec: +p[1] || 0 };
    return s;
  }
  if (clean.startsWith("FLUX,")) { if (s.info) s.info = { ...s.info, minFlux: +clean.slice(5) || 0 }; return s; }
  if (clean.startsWith("LEDS,")) {
    const p = clean.slice(5).split(",");
    if (s.info) s.info = { ...s.info, statusLedEn: p[0] === "1", powerLedEn: p[1] === "1" };
    return s;
  }
  if (clean.startsWith("LASER,")) { s.laserOn = clean.slice(6) === "1"; return s; }
  if (clean.startsWith("SCHED,")) {
    const p = clean.slice(6).split(",");
    if (s.info) s.info = { ...s.info, schedEn: p[0] === "1", schedStart: +p[1] || 0, schedEnd: +p[2] || 0 };
    return s;
  }

  // ── ESP-NOW ─────────────────────────────────────────────────────────────
  if (clean.startsWith("ESPNOW_MODE,")) { s.espNowEn = clean.slice(12) === "1"; return s; }
  if (clean.startsWith("ESP_MAC,")) { s.espMac = clean.slice(8); return s; }
  if (clean.startsWith("ESPNOW_MAC,")) {
    const payload = clean.slice(11).trim();
    const macs = payload ? payload.split("|").map((m) => m.trim()).filter(Boolean) : [];
    s.peers = macs.map((mac) => s.peers.find((p) => p.mac === mac) || { mac, type: null, fw: null, batt: null });
    s.peersFetched = true;
    return s;
  }
  if (clean.startsWith("ESPNOW_RX_INFO,")) {
    const m = clean.slice(15).trim().match(MAC_RE);
    if (m) s.peers = s.peers.map((p) => (p.mac === m[1] ? { ...p, type: m[2] } : p));
    return s;
  }
  if (clean.startsWith("ESPNOW_RX_FW,")) {
    const m = clean.slice(13).trim().match(MAC_RE);
    if (m) s.peers = s.peers.map((p) => (p.mac === m[1] ? { ...p, fw: m[2] } : p));
    return s;
  }
  if (clean.startsWith("TXBATT,")) {
    const rest = clean.slice(7).trim();
    const m = rest.match(MAC_RE);
    if (m) {
      const pct = parseInt(m[2], 10);
      if (pct > 0) s.peers = s.peers.map((p) => (p.mac === m[1] ? { ...p, batt: pct } : p));
    } else {
      const pct = parseInt(rest, 10);
      if (pct > 0 && s.peers.length) s.peers = [{ ...s.peers[0], batt: pct }, ...s.peers.slice(1)];
    }
    return s;
  }

  // ── Camera mode tracking ────────────────────────────────────────────────
  if (clean.startsWith("CAMERA_DIRECT,")) { s.camMode = +clean.slice(14) || 0; return s; }
  if (clean.startsWith("CAM_INFO,")) { s.camMode = +clean.slice(9).split(",")[0] || 0; return s; }

  // ── Pi Camera (mode 7) / webcam (mode 8) ────────────────────────────────
  if (clean === "CAM_PI,CONNECTED") { s.piCam = { ...s.piCam, status: "connected" }; return s; }
  if (clean === "CAM_PI,NOT_FOUND") { s.piCam = { ...s.piCam, status: "not_found" }; return s; }
  if (clean === "CAM_PI,DISCONNECTED") { s.piCam = { ...initialState.piCam }; return s; }
  if (clean.startsWith("CAM_PI_INFO,")) { s.piCam = { ...s.piCam, model: clean.slice(12) }; return s; }
  if (clean.startsWith("CAM_PI_RESOLUTION,")) { s.piCam = { ...s.piCam, resolution: clean.slice(18) }; return s; }
  if (clean.startsWith("CAM_PI_HAS_AF,")) { s.piCam = { ...s.piCam, hasAf: clean.slice(14) === "1" }; return s; }
  if (clean.startsWith("CAM_PI_AF_MODE,")) { s.piCam = { ...s.piCam, afMode: clean.slice(15) }; return s; }
  if (clean === "CAM_PI_AF_LOCKED") { s.piCam = { ...s.piCam, afState: "locked" }; return s; }
  if (clean === "CAM_PI_AF_FAILED") { s.piCam = { ...s.piCam, afState: "failed" }; return s; }
  if (clean.startsWith("CAM_PI_LENS_POS,")) { s.piCam = { ...s.piCam, lensPos: parseFloat(clean.slice(16)) }; return s; }
  if (clean.startsWith("CAM_PI_EXPOSURE_CURRENT,")) { s.piCam = { ...s.piCam, exposure: clean.slice(24) }; return s; }
  if (clean.startsWith("CAM_PI_GAIN_CURRENT,")) { s.piCam = { ...s.piCam, gain: clean.slice(20) }; return s; }
  if (clean.startsWith("CAM_PI_AWB_CURRENT,")) { s.piCam = { ...s.piCam, awb: clean.slice(19) }; return s; }
  if (clean === "CAM_WEBCAM,CONNECTED") { s.webcam = { ...s.webcam, status: "connected" }; return s; }
  if (clean === "CAM_WEBCAM,NOT_FOUND") { s.webcam = { ...s.webcam, status: "not_found" }; return s; }
  if (clean === "CAM_WEBCAM,DISCONNECTED") { s.webcam = { ...initialState.webcam }; return s; }
  if (clean.startsWith("CAM_WEBCAM_INFO,")) { s.webcam = { ...s.webcam, model: clean.slice(16) }; return s; }
  if (clean.startsWith("CAM_WEBCAM_RESOLUTION,")) { s.webcam = { ...s.webcam, resolution: clean.slice(22) }; return s; }

  // ── USB camera ──────────────────────────────────────────────────────────
  if (clean.startsWith("CAM_USB_INFO,")) {
    const p = clean.slice(13).split(",");
    s.camera = { ...s.camera, status: "connected", model: p[0], batt: p[1], remaining: p[2] };
    return s;
  }
  if (clean === "CAM_USB,CONNECTED") { s.camera = { ...s.camera, status: "connected" }; return s; }
  if (clean === "CAM_USB,DISCONNECTED") { s.camera = { ...initialState.camera, status: "off" }; return s; }
  if (clean === "CAM_USB,NOT_FOUND") { s.camera = { ...s.camera, status: "not_found" }; return s; }
  if (clean.startsWith("CAM_ISO_CHOICES,")) { s.camera = { ...s.camera, choices: { ...s.camera.choices, iso: clean.slice(16).split(",").filter(Boolean) } }; return s; }
  if (clean.startsWith("CAM_APERTURE_CHOICES,")) { s.camera = { ...s.camera, choices: { ...s.camera.choices, aperture: clean.slice(21).split(",").filter(Boolean) } }; return s; }
  if (clean.startsWith("CAM_SS_CHOICES,")) { s.camera = { ...s.camera, choices: { ...s.camera.choices, ss: clean.slice(15).split(",").filter(Boolean) } }; return s; }
  if (clean.startsWith("CAM_ISO_CURRENT,")) { s.camera = { ...s.camera, current: { ...s.camera.current, iso: clean.slice(16) } }; return s; }
  if (clean.startsWith("CAM_APERTURE_CURRENT,")) { s.camera = { ...s.camera, current: { ...s.camera.current, aperture: clean.slice(21) } }; return s; }
  if (clean.startsWith("CAM_SS_CURRENT,")) { s.camera = { ...s.camera, current: { ...s.camera.current, ss: clean.slice(15) } }; return s; }
  if (clean.startsWith("CAM_SHUTTER,")) { s.camera = { ...s.camera, shutterResult: clean.slice(12) }; return s; }
  if (clean === "CAM_ERR,TRIGGER_FAIL") { s.camera = { ...s.camera, triggerError: true }; return s; }
  if (clean.startsWith("CAM_ERR,")) { return s; } // other camera errors: logged but no state change

  // ── Google Drive ────────────────────────────────────────────────────────
  if (clean.startsWith("GDRIVE_STATUS,")) {
    const p = clean.slice(14).split(",");
    s.gdrive = { ...s.gdrive, state: p[0], folder: p[1] || "", total: +p[2] || 0 };
    return s;
  }
  if (clean.startsWith("GDRIVE_AUTH_URL,")) {
    const p = clean.slice(16).split(",");
    s.gdrive = { ...s.gdrive, authUrl: p[0], authCode: p[1] || null, error: null };
    return s;
  }
  if (clean.startsWith("GDRIVE_AUTH_OK,")) { s.gdrive = { ...s.gdrive, state: "authorized", email: clean.slice(15), authUrl: null, authCode: null }; return s; }
  if (clean === "GDRIVE_AUTH_PENDING") { s.gdrive = { ...s.gdrive, state: "auth_pending" }; return s; }
  if (clean === "GDRIVE_AUTH_REQUIRED") { s.gdrive = { ...s.gdrive, state: "auth_required" }; return s; }
  if (clean === "GDRIVE_AUTH_EXPIRED") { s.gdrive = { ...s.gdrive, state: "auth_required", authUrl: null, authCode: null, error: "Authorization expired — try again" }; return s; }
  if (clean === "GDRIVE_AUTH_CANCELLED") { s.gdrive = { ...s.gdrive, authUrl: null, authCode: null }; return s; }
  if (clean.startsWith("GDRIVE_PROGRESS,")) {
    const p = clean.slice(16).split(",");
    s.gdrive = { ...s.gdrive, progress: { done: +p[0] || 0, total: +p[1] || 1 }, error: null };
    return s;
  }
  if (clean.startsWith("GDRIVE_DONE,")) { s.gdrive = { ...s.gdrive, progress: null, lastDone: +clean.slice(12) || 0 }; return s; }
  if (clean.startsWith("GDRIVE_ERR,")) { s.gdrive = { ...s.gdrive, progress: null, error: clean.slice(11) }; return s; }

  // ── Time-lapse ──────────────────────────────────────────────────────────
  if (clean.startsWith("TIMELAPSE,")) {
    const p = clean.slice(10).split(",");
    s.timelapse = { state: p[0], shots: +p[1] || 0, remainingSec: +p[2] || 0 };
    return s;
  }
  if (clean === "TIMELAPSE_STOPPING") { s.timelapse = { ...s.timelapse, state: "idle" }; return s; }

  // ── Astro ───────────────────────────────────────────────────────────────
  if (clean.startsWith("ASTRO_LOCATION,")) {
    const p = clean.slice(15).split(",");
    s.astro = { ...s.astro, lat: parseFloat(p[0]), lon: parseFloat(p[1]), error: null };
    return s;
  }
  if (clean === "ASTRO_PREVIEW_BEGIN") { s.astro = { ...s.astro, preview: [] }; return s; }
  if (clean.startsWith("ASTRO_TIMES,")) {
    const p = clean.slice(12).split(",");
    s.astro = { ...s.astro, preview: [...s.astro.preview, { event: p[0], time: p[1], date: p[2] }] };
    return s;
  }
  if (clean === "ASTRO_SCHED_LIST_BEGIN") { s.astro = { ...s.astro, schedules: [] }; return s; }
  if (clean.startsWith("ASTRO_SCHED,")) {
    const p = clean.slice(12).split(",");
    const entry = { id: p[0], event: p[1], offsetMin: +p[2] || 0, durationSec: +p[3] || 0, intervalSec: +p[4] || 0 };
    const exists = s.astro.schedules.some((e) => e.id === entry.id);
    s.astro = { ...s.astro, schedules: exists ? s.astro.schedules.map((e) => (e.id === entry.id ? entry : e)) : [...s.astro.schedules, entry] };
    return s;
  }
  if (clean === "ASTRO_SCHED_CLEARED") { s.astro = { ...s.astro, schedules: [] }; return s; }
  if (clean.startsWith("ASTRO_ERR,")) { s.astro = { ...s.astro, error: clean.slice(10) }; return s; }

  return s;
}

function reducer(state, action) {
  switch (action.type) {
    case "connected": return { ...state, connected: action.value };
    case "telemetry": {
      // flux/temp only — the /data loop owns distance + chart history
      const { flux, temp } = action.value;
      return { ...state, flux, temp };
    }
    case "distance":
      return { ...state, distance: action.value, history: [...state.history.slice(-(MAX_HISTORY - 1)), action.value] };
    case "lines": return action.value.reduce(parseLine, state);
    default: return state;
  }
}

export function usePiTX() {
  const [state, dispatch] = useReducer(reducer, initialState);
  const connectedRef = useRef(false);
  const bootstrappedRef = useRef(false);
  const errorsRef = useRef(0);

  const sendCmd = useCallback(async (cmd) => {
    try {
      const res = await fetch("/cmd", { method: "POST", body: cmd, headers: { "Content-Type": "text/plain" } });
      const text = await res.text();
      const lines = text.split("\n").filter((l) => l.trim() && l.trim() !== "OK");
      if (lines.length) dispatch({ type: "lines", value: lines });
    } catch {
      /* poll loop handles disconnect state */
    }
  }, []);

  // Bootstrap queries once connected
  useEffect(() => {
    if (state.connected && !bootstrappedRef.current) {
      bootstrappedRef.current = true;
      [
        "GET_INFO", "GET_SETTINGS", "ESPNOW_GET_MAC", "GET_CAM_INFO",
        "GDRIVE_STATUS", "TIMELAPSE_STATUS", "ASTRO_SCHEDULE_LIST",
      ].forEach((c, i) => setTimeout(() => sendCmd(c), i * 120));
    }
    if (!state.connected) bootstrappedRef.current = false;
  }, [state.connected, sendCmd]);

  // /data poll loop — distance + queued response lines (matches WiFiTransport)
  useEffect(() => {
    let stopped = false;
    let inFlight = false;
    const tick = async () => {
      if (stopped || inFlight) return;
      inFlight = true;
      try {
        const res = await fetch("/data", { cache: "no-store" });
        const text = await res.text();
        errorsRef.current = 0;
        if (!connectedRef.current) { connectedRef.current = true; dispatch({ type: "connected", value: true }); }
        const lines = text.split("\n");
        const dist = parseInt(lines[0], 10);
        if (!Number.isNaN(dist)) dispatch({ type: "distance", value: dist });
        const rest = lines.slice(1).filter((l) => l.trim());
        if (rest.length) dispatch({ type: "lines", value: rest });
      } catch {
        errorsRef.current += 1;
        if (errorsRef.current >= 5 && connectedRef.current) {
          connectedRef.current = false;
          dispatch({ type: "connected", value: false });
        }
      } finally {
        inFlight = false;
      }
    };
    const id = setInterval(tick, POLL_MS);
    tick();
    return () => { stopped = true; clearInterval(id); };
  }, []);

  // /telemetry poll loop — flux + temp (Pi-only JSON endpoint)
  useEffect(() => {
    let stopped = false;
    const tick = async () => {
      if (stopped) return;
      try {
        const res = await fetch("/telemetry", { cache: "no-store" });
        const j = await res.json();
        dispatch({ type: "telemetry", value: j });
      } catch { /* /data loop owns connection state */ }
    };
    const id = setInterval(tick, TELEM_MS);
    tick();
    return () => { stopped = true; clearInterval(id); };
  }, []);

  return { state, sendCmd };
}
