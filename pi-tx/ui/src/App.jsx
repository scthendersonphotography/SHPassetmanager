import React, { useState } from "react";
import { usePiTX } from "./usePiTX.js";
import TelemetryPanel from "./panels/TelemetryPanel.jsx";
import TriggerPanel from "./panels/TriggerPanel.jsx";
import EspNowPanel from "./panels/EspNowPanel.jsx";
import CameraPanel from "./panels/CameraPanel.jsx";
import GDrivePanel from "./panels/GDrivePanel.jsx";
import TimelapsePanel from "./panels/TimelapsePanel.jsx";
import AstroPanel from "./panels/AstroPanel.jsx";

export default function App() {
  const { state, sendCmd } = usePiTX();
  const [showLog, setShowLog] = useState(false);

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <span className="brand-dot" data-ok={state.connected} />
          <h1>PI&nbsp;TX</h1>
          <span className="brand-sub">
            {state.info ? `${state.info.name} · ${state.info.fw}` : "control panel"}
          </span>
        </div>
        <div className="topbar-right">
          <span className={`conn-badge ${state.connected ? "ok" : "bad"}`}>
            {state.connected ? "CONNECTED" : "OFFLINE"}
          </span>
          <button className="btn ghost" onClick={() => setShowLog((v) => !v)}>
            {showLog ? "Hide log" : "Log"}
          </button>
        </div>
      </header>

      {!state.connected && (
        <div className="offline-note">
          Waiting for the Pi TX daemon… make sure this page is served from the Pi
          (http://pi-tx.local) and the pi-tx service is running.
        </div>
      )}

      <main className="grid">
        <TelemetryPanel state={state} />
        <TriggerPanel state={state} sendCmd={sendCmd} />
        <EspNowPanel state={state} sendCmd={sendCmd} />
        <CameraPanel state={state} sendCmd={sendCmd} />
        <GDrivePanel state={state} sendCmd={sendCmd} />
        <TimelapsePanel state={state} sendCmd={sendCmd} />
        <AstroPanel state={state} sendCmd={sendCmd} />
      </main>

      {showLog && (
        <section className="panel log-panel">
          <h2>Response log</h2>
          <pre>{state.log.slice().reverse().join("\n") || "— no responses yet —"}</pre>
        </section>
      )}
    </div>
  );
}
