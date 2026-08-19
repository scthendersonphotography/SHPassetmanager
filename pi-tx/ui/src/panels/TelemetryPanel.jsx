import React, { useEffect, useRef } from "react";

/** Live telemetry: big distance readout, flux, temp + canvas chart of last 60 readings. */
export default function TelemetryPanel({ state }) {
  const canvasRef = useRef(null);

  useEffect(() => {
    const cv = canvasRef.current;
    if (!cv) return;
    const ctx = cv.getContext("2d");
    const w = (cv.width = cv.clientWidth * devicePixelRatio);
    const h = (cv.height = cv.clientHeight * devicePixelRatio);
    ctx.clearRect(0, 0, w, h);
    const hist = state.history;
    if (hist.length < 2) return;
    const max = Math.max(...hist, 1);
    const min = Math.min(...hist, 0);
    const span = Math.max(max - min, 1);
    ctx.beginPath();
    hist.forEach((v, i) => {
      const x = (i / (hist.length - 1)) * w;
      const y = h - ((v - min) / span) * (h * 0.85) - h * 0.075;
      i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
    });
    ctx.strokeStyle = "#4ade80";
    ctx.lineWidth = 2 * devicePixelRatio;
    ctx.stroke();
  }, [state.history]);

  return (
    <section className="panel telemetry">
      <h2>Telemetry</h2>
      <div className="distance-big">
        {state.distance}
        <span className="unit">cm</span>
      </div>
      <div className="telemetry-row">
        <div className="stat">
          <span className="stat-label">FLUX</span>
          <span className="stat-value">{state.flux}</span>
        </div>
        <div className="stat">
          <span className="stat-label">TEMP</span>
          <span className="stat-value">{state.temp}°C</span>
        </div>
        <div className="stat">
          <span className="stat-label">MIN FLUX</span>
          <span className="stat-value">{state.info?.minFlux ?? "—"}</span>
        </div>
      </div>
      <canvas ref={canvasRef} className="chart" />
    </section>
  );
}
