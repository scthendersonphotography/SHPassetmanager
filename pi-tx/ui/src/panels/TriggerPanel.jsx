import React, { useEffect, useState } from "react";

/** Trigger & wake controls: fire, modes, pulse duration, polarity, wake, schedule window. */
export default function TriggerPanel({ state, sendCmd }) {
  const st = state.settings;
  const info = state.info;

  // Local editable copies (synced from device once known)
  const [pulse, setPulse] = useState(100);
  const [shots, setShots] = useState(1);
  const [cooldown, setCooldown] = useState(0);
  const [delayShot, setDelayShot] = useState(0);
  const [minDist, setMinDist] = useState(0);
  const [maxDist, setMaxDist] = useState(0);
  const [invert, setInvert] = useState(false);
  const [wakeSec, setWakeSec] = useState(300);
  const [schedStart, setSchedStart] = useState("00:00");
  const [schedEnd, setSchedEnd] = useState("23:59");

  useEffect(() => {
    if (!st) return;
    setPulse(st.pulseDur); setShots(st.shots); setCooldown(st.cooldown);
    setDelayShot(st.delayShot); setMinDist(st.minDist); setMaxDist(st.maxDist);
    setInvert(st.invertTrig);
  }, [st]);
  useEffect(() => {
    if (!info) return;
    setWakeSec(info.wakeSec);
    setSchedStart(minToHHMM(info.schedStart));
    setSchedEnd(minToHHMM(info.schedEnd));
  }, [info]);

  // Settings form is authoritative only after the device's S,... line arrives
  // (GET_SETTINGS is sent at bootstrap). Never allow applying placeholder
  // defaults — that would silently overwrite the Pi's stored configuration.
  const settingsLoaded = st != null;
  const applySettings = () =>
    settingsLoaded &&
    sendCmd(`S,${minDist},${maxDist},${cooldown},${shots},${pulse},${invert ? 1 : 0},${delayShot}`);

  return (
    <section className="panel">
      <h2>Trigger &amp; Wake</h2>

      <div className="fire-row">
        <button className="btn fire" onClick={() => sendCmd("T")}>⚡ FIRE</button>
        <button className="btn" onClick={() => sendCmd("T_WAKE")}>Wake pulse</button>
      </div>

      <div className="field-row">
        <label>Trigger output</label>
        <div className="seg">
          <button className={info?.trigMode === 0 ? "on" : ""} onClick={() => sendCmd("TRIG_MODE,0")}>DRY</button>
          <button className={info?.trigMode === 1 ? "on" : ""} onClick={() => sendCmd("TRIG_MODE,1")}>HOT</button>
        </div>
      </div>
      <div className="field-row">
        <label>Wake output</label>
        <div className="seg">
          <button className={info?.wakeMode === 0 ? "on" : ""} onClick={() => sendCmd("WAKE_MODE,0")}>DRY</button>
          <button className={info?.wakeMode === 1 ? "on" : ""} onClick={() => sendCmd("WAKE_MODE,1")}>HOT</button>
        </div>
      </div>

      <div className="field-grid">
        <Num label="Pulse (ms)" value={pulse} set={setPulse} min={100} max={5000} />
        <Num label="Shots" value={shots} set={setShots} min={1} max={50} />
        <Num label="Cooldown (ms)" value={cooldown} set={setCooldown} min={0} max={600000} />
        <Num label="Delay (ms)" value={delayShot} set={setDelayShot} min={0} max={5000} />
        <Num label="Min dist (cm)" value={minDist} set={setMinDist} min={0} max={65535} />
        <Num label="Max dist (cm)" value={maxDist} set={setMaxDist} min={0} max={65535} />
      </div>
      <div className="field-row">
        <label>
          <input type="checkbox" checked={invert} onChange={(e) => setInvert(e.target.checked)} />
          &nbsp;Invert polarity
        </label>
        <button className="btn primary" disabled={!settingsLoaded} onClick={applySettings}>
          {settingsLoaded ? "Apply settings" : "Loading settings…"}
        </button>
      </div>

      <hr />
      <div className="field-row">
        <label>
          <input
            type="checkbox"
            checked={info?.wakeEn ?? false}
            onChange={(e) => sendCmd(`W,${e.target.checked ? 1 : 0},${wakeSec}`)}
          />
          &nbsp;Wake timer
        </label>
        <span className="inline-num">
          every <input type="number" value={wakeSec} min={1} max={86400}
            onChange={(e) => setWakeSec(+e.target.value || 1)}
            onBlur={() => info?.wakeEn && sendCmd(`W,1,${wakeSec}`)} /> s
        </span>
      </div>

      <div className="field-row">
        <label>
          <input
            type="checkbox"
            checked={info?.schedEn ?? false}
            onChange={(e) =>
              sendCmd(`SCHED,${e.target.checked ? 1 : 0},${hhmmToMin(schedStart)},${hhmmToMin(schedEnd)}`)
            }
          />
          &nbsp;Daily window
        </label>
        <span className="inline-num">
          <input type="time" value={schedStart} onChange={(e) => setSchedStart(e.target.value)}
            onBlur={() => info?.schedEn && sendCmd(`SCHED,1,${hhmmToMin(schedStart)},${hhmmToMin(schedEnd)}`)} />
          –
          <input type="time" value={schedEnd} onChange={(e) => setSchedEnd(e.target.value)}
            onBlur={() => info?.schedEn && sendCmd(`SCHED,1,${hhmmToMin(schedStart)},${hhmmToMin(schedEnd)}`)} />
        </span>
      </div>

      <div className="field-row">
        <label>
          <input type="checkbox" checked={state.laserOn}
            onChange={(e) => sendCmd(`LASER,${e.target.checked ? 1 : 0}`)} />
          &nbsp;Alignment laser
        </label>
        <label>
          <input type="checkbox" checked={info?.statusLedEn ?? true}
            onChange={(e) => sendCmd(`LEDS,${e.target.checked ? 1 : 0},${info?.powerLedEn ? 1 : 0}`)} />
          &nbsp;Status LED
        </label>
        <label>
          <input type="checkbox" checked={info?.powerLedEn ?? true}
            onChange={(e) => sendCmd(`LEDS,${info?.statusLedEn ? 1 : 0},${e.target.checked ? 1 : 0}`)} />
          &nbsp;Power LED
        </label>
      </div>
    </section>
  );
}

function Num({ label, value, set, min, max }) {
  return (
    <label className="num-field">
      <span>{label}</span>
      <input type="number" value={value} min={min} max={max}
        onChange={(e) => set(e.target.value === "" ? "" : +e.target.value)} />
    </label>
  );
}

const minToHHMM = (m) => `${String(Math.floor(m / 60)).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}`;
const hhmmToMin = (s) => {
  const [h, m] = s.split(":").map((n) => parseInt(n, 10) || 0);
  return h * 60 + m;
};
