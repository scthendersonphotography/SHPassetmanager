import React, { useState } from "react";

const MAC_OK = /^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$/;

/** ESP-NOW receivers: peer list with type/fw/battery badges, pair by MAC, remove. */
export default function EspNowPanel({ state, sendCmd }) {
  const [mac, setMac] = useState("");
  const valid = MAC_OK.test(mac.trim());

  const refresh = () => sendCmd("ESPNOW_GET_MAC");

  return (
    <section className="panel">
      <h2>
        ESP-NOW Receivers
        <span className="h-actions">
          <label className="toggle-sm">
            <input type="checkbox" checked={state.espNowEn}
              onChange={(e) => sendCmd(`ESPNOW_MODE,${e.target.checked ? 1 : 0}`)} />
            enabled
          </label>
          <button className="btn ghost" onClick={refresh}>↻</button>
        </span>
      </h2>

      {state.peers.length === 0 ? (
        <p className="muted">{state.peersFetched ? "No receivers paired." : "Loading peer list…"}</p>
      ) : (
        <ul className="peer-list">
          {state.peers.map((p) => (
            <li key={p.mac}>
              <code>{p.mac}</code>
              <span className="badges">
                {p.type && <span className="badge">{p.type}</span>}
                {p.fw && <span className="badge">fw {p.fw}</span>}
                {p.batt != null && <span className="badge batt">{p.batt}%</span>}
              </span>
              <button className="btn danger sm"
                onClick={() => { sendCmd(`ESPNOW_REMOVE,${p.mac}`); setTimeout(refresh, 600); }}>
                Remove
              </button>
            </li>
          ))}
        </ul>
      )}

      <div className="pair-row">
        <input
          placeholder="AA:BB:CC:DD:EE:FF"
          value={mac}
          onChange={(e) => setMac(e.target.value)}
          spellCheck={false}
        />
        <button className="btn primary" disabled={!valid}
          onClick={() => { sendCmd(`ESPNOW_PAIR,${mac.trim().toUpperCase()}`); setMac(""); setTimeout(refresh, 800); }}>
          Pair
        </button>
      </div>
      {state.peers.length > 0 && (
        <button className="btn ghost sm" style={{ marginTop: 8 }}
          onClick={() => { sendCmd("ESPNOW_REMOVE_ALL"); setTimeout(refresh, 600); }}>
          Remove all
        </button>
      )}
    </section>
  );
}
