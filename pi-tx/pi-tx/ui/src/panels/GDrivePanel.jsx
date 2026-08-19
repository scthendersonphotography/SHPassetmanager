import React, { useEffect, useState } from "react";

/** Google Drive: auth status/device-flow, folder, upload buttons, progress bar. */
export default function GDrivePanel({ state, sendCmd }) {
  const g = state.gdrive;
  const [folder, setFolder] = useState("");
  useEffect(() => { setFolder(g.folder || ""); }, [g.folder]);

  const authorized = g.state === "authorized" || g.state === "ready" || g.email;
  const pct = g.progress ? Math.round((g.progress.done / Math.max(g.progress.total, 1)) * 100) : null;

  return (
    <section className="panel">
      <h2>
        Google Drive
        <span className="h-actions">
          <button className="btn ghost sm" onClick={() => sendCmd("GDRIVE_STATUS")}>↻</button>
        </span>
      </h2>

      <div className="field-row">
        <label>Status</label>
        <span className={authorized ? "ok-text" : "muted"}>
          {g.email ? `Authorized — ${g.email}` : g.state || "unknown"}
        </span>
      </div>

      {!authorized && !g.authUrl && (
        <button className="btn primary" onClick={() => sendCmd("GDRIVE_AUTH_URL")}>
          Authorize Google Drive
        </button>
      )}
      {g.authUrl && (
        <div className="auth-box">
          <p>1. Open <a href={g.authUrl} target="_blank" rel="noreferrer">{g.authUrl}</a></p>
          <p>2. Enter code: <code className="auth-code">{g.authCode}</code></p>
          <div className="field-row">
            <button className="btn sm" onClick={() => sendCmd("GDRIVE_AUTH_CODE")}>Check status</button>
            <button className="btn ghost sm" onClick={() => sendCmd("GDRIVE_AUTH_CANCEL")}>Cancel</button>
          </div>
        </div>
      )}

      <div className="field-row">
        <label>Folder</label>
        <input value={folder} placeholder="LaserEye Uploads" onChange={(e) => setFolder(e.target.value)}
          onBlur={() => folder.trim() && folder !== g.folder && sendCmd(`GDRIVE_FOLDER,${folder.trim()}`)} />
      </div>
      <div className="field-row">
        <label>Uploaded total</label>
        <span>{g.total}</span>
      </div>

      <div className="fire-row">
        <button className="btn" disabled={!!g.progress} onClick={() => sendCmd("GDRIVE_UPLOAD_LAST,50")}>
          Upload last 50
        </button>
        <button className="btn" disabled={!!g.progress} onClick={() => sendCmd("GDRIVE_UPLOAD_ALL")}>
          Upload all new
        </button>
      </div>

      {pct != null && (
        <div className="progress">
          <div className="progress-bar" style={{ width: `${pct}%` }} />
          <span className="progress-text">{pct}% ({g.progress.done}/{g.progress.total})</span>
        </div>
      )}
      {g.lastDone != null && !g.progress && (
        <p className="ok-text">Last upload: {g.lastDone} file{g.lastDone === 1 ? "" : "s"}</p>
      )}
      {g.error && <p className="warn">Error: {g.error}</p>}
    </section>
  );
}
