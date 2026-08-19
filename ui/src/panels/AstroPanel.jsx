import React, { useState } from "react";
import { ASTRO_EVENTS } from "../usePiTX.js";

/** Astronomical triggers: location, event picker + offset, preview times, schedule list. */
export default function AstroPanel({ state, sendCmd }) {
  const a = state.astro;
  const [lat, setLat] = useState("");
  const [lon, setLon] = useState("");
  const [event, setEvent] = useState("SUNRISE");
  const [offset, setOffset] = useState(0);
  const [duration, setDuration] = useState(3600);
  const [interval_, setInterval_] = useState(10);

  const hasLocation = a.lat != null;

  const setLocation = (la, lo) => {
    sendCmd(`ASTRO_LOCATION,${la},${lo}`);
    setTimeout(() => sendCmd("ASTRO_PREVIEW"), 500);
  };

  const useMyLocation = () => {
    navigator.geolocation?.getCurrentPosition(
      (pos) => {
        const la = pos.coords.latitude.toFixed(5);
        const lo = pos.coords.longitude.toFixed(5);
        setLat(la); setLon(lo);
        setLocation(la, lo);
      },
      () => {},
    );
  };

  return (
    <section className="panel wide">
      <h2>Astronomical Triggers</h2>

      <div className="field-row">
        <label>Location</label>
        {hasLocation && <span className="muted">{a.lat?.toFixed(4)}, {a.lon?.toFixed(4)}</span>}
        <input className="coord" placeholder="lat" value={lat} onChange={(e) => setLat(e.target.value)} />
        <input className="coord" placeholder="lon" value={lon} onChange={(e) => setLon(e.target.value)} />
        <button className="btn sm" disabled={!lat || !lon} onClick={() => setLocation(lat, lon)}>Set</button>
        <button className="btn ghost sm" onClick={useMyLocation}>📍 Use my location</button>
      </div>

      <div className="event-picker">
        {ASTRO_EVENTS.map((ev) => (
          <button key={ev} className={`chip ${event === ev ? "on" : ""}`} onClick={() => setEvent(ev)}>
            {ev.replace(/_/g, " ")}
          </button>
        ))}
      </div>

      <div className="field-grid">
        <label className="num-field"><span>Offset (min)</span>
          <input type="number" value={offset} onChange={(e) => setOffset(+e.target.value || 0)} /></label>
        <label className="num-field"><span>Duration (s)</span>
          <input type="number" min={1} value={duration} onChange={(e) => setDuration(+e.target.value || 1)} /></label>
        <label className="num-field"><span>Interval (s)</span>
          <input type="number" min={1} value={interval_} onChange={(e) => setInterval_(+e.target.value || 1)} /></label>
      </div>

      <div className="fire-row">
        <button className="btn primary" disabled={!hasLocation}
          onClick={() => {
            sendCmd(`ASTRO_EVENT,${event},${offset},${duration},${interval_}`);
            setTimeout(() => sendCmd("ASTRO_SCHEDULE_LIST"), 500);
          }}>
          + Schedule
        </button>
        <button className="btn ghost" disabled={!hasLocation} onClick={() => sendCmd("ASTRO_PREVIEW")}>
          Preview times
        </button>
      </div>
      {!hasLocation && <p className="muted">Set a location to enable astronomical scheduling.</p>}
      {a.error && <p className="warn">Error: {a.error}</p>}

      {a.preview.length > 0 && (
        <>
          <h3>Upcoming event times</h3>
          <div className="preview-grid">
            {a.preview.map((p, i) => (
              <div key={i} className="preview-item">
                <span className="badge">{p.event.replace(/_/g, " ")}</span>
                <span>{p.time}</span>
                <span className="muted">{p.date}</span>
              </div>
            ))}
          </div>
        </>
      )}

      <h3>
        Saved schedules
        <span className="h-actions">
          {a.schedules.length > 0 && (
            <button className="btn danger sm" onClick={() => sendCmd("ASTRO_SCHEDULE_CLEAR")}>Clear all</button>
          )}
        </span>
      </h3>
      {a.schedules.length === 0 ? (
        <p className="muted">No schedules yet.</p>
      ) : (
        <ul className="peer-list">
          {a.schedules.map((sc) => (
            <li key={sc.id}>
              <span className="badge">{sc.event.replace(/_/g, " ")}</span>
              <span>
                {sc.offsetMin >= 0 ? "+" : ""}{sc.offsetMin} min · {sc.durationSec}s @ every {sc.intervalSec}s
              </span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
