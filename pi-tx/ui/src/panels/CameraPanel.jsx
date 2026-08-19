import React, { useCallback, useEffect, useRef, useState } from "react";

/**
 * Camera panel — three selectable sources on the Pi TX:
 *   DSLR (mode 6, gphoto2)   — full ISO/aperture/shutter control
 *   Pi Camera (mode 7, CSI)  — fixed exposure, resolution only
 *   Webcam (mode 8, V4L2)    — fixed exposure, resolution only
 */
const SOURCES = [
  { mode: 6, label: "USB DSLR" },
  { mode: 7, label: "Pi Camera" },
  { mode: 8, label: "Webcam" },
];

const PI_RES_CHOICES     = ["1080p", "4K"];
const WEBCAM_RES_CHOICES = ["720p", "1080p"];

export default function CameraPanel({ state, sendCmd }) {
  const mode = state.camMode;

  return (
    <section className="panel">
      <h2>
        Camera
        <span className="h-actions">
          {mode !== 0 && (
            <button className="btn ghost sm" onClick={() => sendCmd("CAMERA_DIRECT,0")}>Disconnect</button>
          )}
        </span>
      </h2>

      {/* Source selector */}
      <div className="field-row" style={{ gap: 6, flexWrap: "wrap" }}>
        {SOURCES.map((src) => (
          <button
            key={src.mode}
            className={`btn sm ${mode === src.mode ? "primary" : "ghost"}`}
            onClick={() => { if (mode !== src.mode) sendCmd(`CAMERA_DIRECT,${src.mode}`); }}
          >
            {src.label}
          </button>
        ))}
      </div>

      {mode === 6 && <DslrSection cam={state.camera} sendCmd={sendCmd} />}
      {mode === 7 && (
        <DirectSection
          cam={state.piCam}
          sendCmd={sendCmd}
          resChoices={PI_RES_CHOICES}
          notFoundMsg="No Pi Camera found — check the CSI ribbon cable and that the camera is enabled."
          shutterResult={state.camera.shutterResult}
          showPreview
        />
      )}
      {mode === 8 && (
        <DirectSection
          cam={state.webcam}
          sendCmd={sendCmd}
          resChoices={WEBCAM_RES_CHOICES}
          notFoundMsg="No webcam found — plug a USB webcam into the Pi."
          shutterResult={state.camera.shutterResult}
        />
      )}
      {mode === 0 && (
        <p className="muted">Select a camera source to connect.</p>
      )}
    </section>
  );
}

/**
 * Live preview image from /preview.jpg, refreshed at ~1 fps.
 * Only shown when the Pi Camera (mode 7) is connected.
 *
 * States:
 *   "loading" — waiting for the first frame (initial 10 s window)
 *   "ok"      — at least one frame received; shows the live image
 *   "error"   — 10 s elapsed with no successful frame; shows a fix hint
 *
 * Recovers automatically: if /preview.jpg starts responding again after an
 * error the component transitions back to "ok" on the next successful load.
 */
function PiCamPreview() {
  const [src, setSrc] = useState(() => `/preview.jpg?t=${Date.now()}`);
  // "loading" | "ok" | "error"
  const [status, setStatus] = useState("loading");
  const timerRef  = useRef(null);
  // Mirror of status in a ref so event-handler callbacks see the latest value
  // without needing to be re-created on every render.
  const statusRef = useRef("loading");

  const clearTimer = useCallback(() => {
    if (timerRef.current) { clearTimeout(timerRef.current); timerRef.current = null; }
  }, []);

  /** Arm (or re-arm) the 10 s failure timer. */
  const armTimer = useCallback(() => {
    clearTimer();
    timerRef.current = setTimeout(() => {
      timerRef.current = null;
      setStatus("error");
      statusRef.current = "error";
    }, 10000);
  }, [clearTimer]);

  // Start the polling interval and arm the initial failure timer on mount.
  useEffect(() => {
    armTimer();
    const id = setInterval(() => setSrc(`/preview.jpg?t=${Date.now()}`), 1000);
    return () => { clearInterval(id); clearTimer(); };
  }, [armTimer, clearTimer]);

  const handleLoad = useCallback(() => {
    clearTimer();
    statusRef.current = "ok";
    setStatus("ok");
  }, [clearTimer]);

  const handleError = useCallback(() => {
    // If we were showing a live frame, start a 10 s recovery window before
    // surfacing the error (avoids flashing on a single dropped frame).
    if (statusRef.current === "ok") {
      statusRef.current = "recovering";
      armTimer();
    }
    // "loading"    — initial timer already running, nothing extra needed.
    // "recovering" — timer already running.
    // "error"      — already failed; keep polling silently for auto-recovery.
  }, [armTimer]);

  return (
    <div
      style={{
        margin: "10px 0 6px",
        background: "#000",
        borderRadius: 4,
        overflow: "hidden",
        aspectRatio: "16 / 9",
        position: "relative",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
      }}
    >
      {status === "loading" && (
        <span style={{ color: "#555", fontSize: 11, fontFamily: "monospace", position: "absolute" }}>
          Preview loading…
        </span>
      )}
      {status === "error" && (
        <span style={{
          color: "#f87171", fontSize: 11, fontFamily: "monospace",
          position: "absolute", textAlign: "center", padding: "0 16px", lineHeight: 1.6,
        }}>
          ⚠ Preview unavailable — no frames received.<br />
          Check the CSI ribbon cable, then reload the Pi Camera source.
        </span>
      )}
      <img
        src={src}
        alt="Pi Camera preview"
        style={{
          width: "100%", height: "100%", objectFit: "contain", display: "block",
          // Hide broken/blank image while in loading or error state so the
          // overlaid message shows clearly on the black background.
          opacity: status === "ok" ? 1 : 0,
        }}
        onLoad={handleLoad}
        onError={handleError}
      />
    </div>
  );
}

/** Pi Camera / webcam — status, resolution picker, test shutter. */
function DirectSection({ cam, sendCmd, resChoices, notFoundMsg, shutterResult, showPreview }) {
  const connected = cam.status === "connected";
  return (
    <>
      {cam.status === "not_found" && <p className="warn">{notFoundMsg}</p>}
      {!connected && cam.status !== "not_found" && <p className="muted">Connecting…</p>}
      {connected && (
        <>
          <div className="telemetry-row">
            <div className="stat"><span className="stat-label">MODEL</span><span className="stat-value sm">{cam.model || "—"}</span></div>
            <div className="stat"><span className="stat-label">STATUS</span><span className="stat-value sm ok-text">Connected</span></div>
          </div>
          <Setting
            label="Resolution"
            choices={resChoices}
            value={cam.resolution}
            onChange={(v) => sendCmd(`CAM_RESOLUTION,${v}`)}
          />
          {showPreview && <PiCamPreview />}
          {cam.hasAf ? (
            <Cam3Controls cam={cam} sendCmd={sendCmd} />
          ) : (
            <p className="muted" style={{ marginTop: 4 }}>
              Fixed exposure — photos save to the Pi and upload to Google Drive like DSLR shots.
            </p>
          )}
          <TestShutter sendCmd={sendCmd} shutterResult={shutterResult} />
        </>
      )}
    </>
  );
}

const AF_MODES = ["continuous", "auto", "manual"];
const AWB_CHOICES = ["auto", "Daylight", "Cloudy", "Tungsten", "Fluorescent", "Indoor"];

/** Pi Camera 3 (IMX708) — AF, lens position, exposure, gain, AWB. */
function Cam3Controls({ cam, sendCmd }) {
  const [focusing, setFocusing] = useState(false);
  const timerRef = useRef(null);
  const lastAfState = useRef(cam.afState);

  // Clear "Focusing…" on lock/fail response or 5 s timeout
  useEffect(() => {
    if (cam.afState !== lastAfState.current) {
      lastAfState.current = cam.afState;
      setFocusing(false);
      if (timerRef.current) clearTimeout(timerRef.current);
    }
  }, [cam.afState]);
  useEffect(() => () => timerRef.current && clearTimeout(timerRef.current), []);

  const triggerAf = () => {
    setFocusing(true);
    sendCmd("CAM_PI_AF_TRIGGER");
    if (timerRef.current) clearTimeout(timerRef.current);
    timerRef.current = setTimeout(() => setFocusing(false), 5000);
  };

  const exposureAuto = (cam.exposure ?? "auto") === "auto";
  const gainAuto = (cam.gain ?? "auto") === "auto";
  const manualExposure = !(exposureAuto && gainAuto);

  return (
    <>
      {/* AF mode */}
      <div className="field-row">
        <label>Focus</label>
        <div style={{ display: "flex", gap: 4, flexWrap: "wrap" }}>
          {AF_MODES.map((m) => (
            <button
              key={m}
              className={`btn sm ${cam.afMode === m ? "primary" : "ghost"}`}
              onClick={() => sendCmd(`CAM_PI_AF_MODE,${m}`)}
            >
              {m.charAt(0).toUpperCase() + m.slice(1)}
            </button>
          ))}
        </div>
      </div>

      {cam.afMode === "auto" && (
        <div className="field-row">
          <label />
          <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <button className="btn sm" disabled={focusing} onClick={triggerAf}>
              {focusing ? "Focusing…" : "Trigger autofocus"}
            </button>
            {!focusing && cam.afState === "locked" && <span className="ok-text">Focus locked</span>}
            {!focusing && cam.afState === "failed" && <span className="warn">Focus failed</span>}
          </div>
        </div>
      )}

      {cam.afMode === "manual" && (
        <div className="field-row">
          <label>Lens pos</label>
          <div style={{ display: "flex", gap: 8, alignItems: "center", flex: 1 }}>
            <input
              type="range" min="0" max="10" step="0.1"
              value={cam.lensPos ?? 0}
              onChange={(e) => sendCmd(`CAM_PI_LENS_POS,${e.target.value}`)}
              style={{ flex: 1 }}
            />
            <span className="muted" style={{ minWidth: 64, textAlign: "right" }}>
              {(cam.lensPos ?? 0) <= 0 ? "∞" : `${(100 / (cam.lensPos ?? 1)).toFixed(0)} cm`}
            </span>
          </div>
        </div>
      )}

      {/* Exposure — single Auto/Manual mode: the sensor cannot mix auto
          shutter with manual gain (or vice versa), so Manual exposes both. */}
      <div className="field-row">
        <label>Exposure</label>
        <div style={{ display: "flex", gap: 4 }}>
          <button
            className={`btn sm ${manualExposure ? "ghost" : "primary"}`}
            onClick={() => { sendCmd("CAM_PI_EXPOSURE,auto"); sendCmd("CAM_PI_GAIN,auto"); }}
          >
            Auto
          </button>
          <button
            className={`btn sm ${manualExposure ? "primary" : "ghost"}`}
            onClick={() => sendCmd(`CAM_PI_EXPOSURE,${exposureAuto ? "10" : cam.exposure}`)}
          >
            Manual
          </button>
        </div>
      </div>
      {manualExposure && (
        <>
          <ValueRow
            label="Shutter" unit="ms" min={1} max={10000} step={1}
            value={cam.exposure ?? "10"}
            onValue={(v) => sendCmd(`CAM_PI_EXPOSURE,${v}`)}
          />
          <ValueRow
            label="Gain" unit="≈ISO/100" min={1} max={16} step={0.5}
            value={cam.gain ?? "1"}
            onValue={(v) => sendCmd(`CAM_PI_GAIN,${v}`)}
          />
        </>
      )}

      {/* White balance */}
      <div className="field-row">
        <label>White bal</label>
        <select value={cam.awb ?? "auto"} onChange={(e) => sendCmd(`CAM_PI_AWB,${e.target.value}`)}>
          {AWB_CHOICES.map((w) => (
            <option key={w} value={w}>{w === "auto" ? "Auto" : w}</option>
          ))}
        </select>
      </div>
    </>
  );
}

/** Numeric input row committed on blur/Enter (shutter ms / analogue gain). */
function ValueRow({ label, value, unit, min, max, step, onValue }) {
  const [draft, setDraft] = useState(value);
  useEffect(() => { setDraft(value); }, [value]);
  return (
    <div className="field-row">
      <label>{label}</label>
      <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
        <input
          type="number" min={min} max={max} step={step} value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onBlur={() => { if (draft && draft !== value) onValue(draft); }}
          onKeyDown={(e) => { if (e.key === "Enter" && draft) onValue(draft); }}
          style={{ width: 80 }}
        />
        <span className="muted">{unit}</span>
      </div>
    </div>
  );
}

/** USB DSLR (gphoto2) — model/battery, ISO/aperture/shutter, test shutter. */
function DslrSection({ cam, sendCmd }) {
  const connected = cam.status === "connected";
  return (
    <>
      {cam.status === "not_found" && (
        <p className="warn">No USB camera found — check the cable and that the camera is on.</p>
      )}
      {!connected && cam.status !== "not_found" && (
        <p className="muted">Connect a camera over USB, then press USB DSLR.</p>
      )}

      {connected && (
        <>
          <div className="telemetry-row">
            <div className="stat"><span className="stat-label">MODEL</span><span className="stat-value sm">{cam.model || "—"}</span></div>
            <div className="stat"><span className="stat-label">BATT</span><span className="stat-value sm">{cam.batt ?? "—"}%</span></div>
            <div className="stat"><span className="stat-label">SHOTS LEFT</span><span className="stat-value sm">{cam.remaining ?? "—"}</span></div>
          </div>

          <Setting label="ISO" choices={cam.choices.iso} value={cam.current.iso}
            onChange={(v) => sendCmd(`CAM_ISO,${v}`)} />
          <Setting label="Aperture" choices={cam.choices.aperture} value={cam.current.aperture}
            onChange={(v) => sendCmd(`CAM_APERTURE,${v}`)} />
          <Setting label="Shutter" choices={cam.choices.ss} value={cam.current.ss}
            onChange={(v) => sendCmd(`CAM_SS,${v}`)} />

          <TestShutter sendCmd={sendCmd} shutterResult={cam.shutterResult} />
          {cam.triggerError && (
            <p className="warn" style={{ marginTop: 6 }}>
              ⚠️ Camera trigger failed — a shot may have been missed. Check the USB connection.
            </p>
          )}
        </>
      )}
    </>
  );
}

function TestShutter({ sendCmd, shutterResult }) {
  return (
    <div className="field-row" style={{ marginTop: 10 }}>
      <button className="btn primary" onClick={() => sendCmd("CAMERA_TEST_SHUTTER")}>
        📸 Test shutter
      </button>
      {shutterResult && (
        <span className={shutterResult === "OK" ? "ok-text" : "warn"}>
          {shutterResult === "OK" ? "Shutter fired" : `Shutter: ${shutterResult}`}
        </span>
      )}
    </div>
  );
}

function Setting({ label, choices, value, onChange }) {
  return (
    <div className="field-row">
      <label>{label}</label>
      <select value={value ?? ""} disabled={!choices.length} onChange={(e) => onChange(e.target.value)}>
        {!choices.length && <option value="">—</option>}
        {value != null && !choices.includes(value) && <option value={value}>{value}</option>}
        {choices.map((c) => <option key={c} value={c}>{c}</option>)}
      </select>
    </div>
  );
}
