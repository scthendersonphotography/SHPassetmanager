import React, { useEffect, useRef, useState } from "react";

/** Time-lapse: interval/duration inputs, start/stop, live shot counter + countdown. */
export default function TimelapsePanel({ state, sendCmd }) {
  const tl = state.timelapse;
  const running = tl.state === "running";
  const [interval_, setInterval_] = useState(10);
  const [duration, setDuration] = useState(3600);

  // Poll status every 2 s while running so the counter stays live
  const runningRef = useRef(running);
  runningRef.current = running;
  useEffect(() => {
    const id = setInterval(() => {
      if (runningRef.current) sendCmd("TIMELAPSE_STATUS");
    }, 2000);
    return () => clearInterval(id);
  }, [sendCmd]);

  return (
    <section className="panel">
      <h2>Time-lapse</h2>

      <div className="field-grid">
        <label className="num-field">
          <span>Interval (s)</span>
          <input type="number" min={1} value={interval_} disabled={running}
            onChange={(e) => setInterval_(+e.target.value || 1)} />
        </label>
        <label className="num-field">
          <span>Duration (s)</span>
          <input type="number" min={1} value={duration} disabled={running}
            onChange={(e) => setDuration(+e.target.value || 1)} />
        </label>
      </div>

      <div className="fire-row">
        {running ? (
          <button className="btn danger" onClick={() => sendCmd("TIMELAPSE_STOP")}>■ Stop</button>
        ) : (
          <button className="btn primary" onClick={() => sendCmd(`TIMELAPSE_START,${interval_},${duration}`)}>
            ▶ Start
          </button>
        )}
        <button className="btn ghost" onClick={() => sendCmd("TIMELAPSE_STATUS")}>↻</button>
      </div>

      <div className="telemetry-row">
        <div className="stat">
          <span className="stat-label">STATE</span>
          <span className={`stat-value sm ${running ? "ok-text" : ""}`}>{tl.state}</span>
        </div>
        <div className="stat">
          <span className="stat-label">SHOTS</span>
          <span className="stat-value sm">{tl.shots}</span>
        </div>
        <div className="stat">
          <span className="stat-label">REMAINING</span>
          <span className="stat-value sm">{fmtSec(tl.remainingSec)}</span>
        </div>
      </div>
    </section>
  );
}

const fmtSec = (s) => {
  if (!s) return "0:00";
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}` : `${m}:${String(sec).padStart(2, "0")}`;
};
