import { useEffect, useState } from "react";
import { Navigate } from "react-router-dom";
import { api, apiUrl } from "../api";
import { stamp } from "../utils";

const STEPS = [
  ["connecting", "Connecting to Portal"],
  ["awaiting_login", "Waiting for administrator login"],
  ["loading_report", "Loading Attendance Report"],
  ["discovering_dates", "Discovering Dates"],
  ["processing_dates", "Processing Dates"],
  ["extracting_records", "Extracting Candidate Records"],
  ["reconciling", "Calculating attendance"],
  ["saving", "Saving Records"],
  ["completed", "Sync Completed"],
];

export default function SyncPage() {
  const [role, setRole] = useState(null);
  const [current, setCurrent] = useState(null);
  const [history, setHistory] = useState([]);
  const [mode, setMode] = useState("incremental");
  const [dateFrom, setDateFrom] = useState("");
  const [dateTo, setDateTo] = useState("");
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [files, setFiles] = useState([]);
  const [showExcel, setShowExcel] = useState(false);
  const [cloudBlocked, setCloudBlocked] = useState(false);
  const [batchCode, setBatchCode] = useState("");
  const [remoteBrowser, setRemoteBrowser] = useState(false);
  const [remoteBrowserPath, setRemoteBrowserPath] = useState("");
  const [showLive, setShowLive] = useState(true);

  const refresh = () => {
    api("/api/sync/current").then((payload) => setCurrent(payload.sync)).catch((err) => setError(err.message));
    api("/api/sync/history").then((payload) => setHistory(payload.history)).catch(() => {});
  };

  useEffect(() => {
    api("/api/bootstrap")
      .then((payload) => {
        setRole(payload.user?.role || "admin");
        setCloudBlocked(Boolean(payload.cloud_sync_blocked));
        setRemoteBrowser(Boolean(payload.remote_browser));
        setRemoteBrowserPath(payload.remote_browser_path || "");
        setBatchCode(payload.batch?.batch_code || "");
      })
      .catch(() => setRole("admin"));
    refresh();
    const timer = setInterval(refresh, 1500);
    return () => clearInterval(timer);
  }, []);

  if (role === "centre") {
    return <Navigate to="/" replace />;
  }

  const running = ["running", "awaiting_login", "cancelling"].includes(current?.status);
  const completed = current?.status === "completed";
  const stepIndex = STEPS.findIndex((step) => step[0] === current?.progress_step);
  const browserSrc = remoteBrowserPath ? apiUrl(remoteBrowserPath) : "";

  async function startLive() {
    setError("");
    setMessage("");
    if (mode === "date" && !dateFrom) {
      setError("Choose the date to check.");
      return;
    }
    if (mode === "range" && (!dateFrom || !dateTo)) {
      setError("Choose the start date and the end date.");
      return;
    }
    const payloadMode = mode === "date" ? "range" : mode;
    const from = mode === "incremental" ? null : dateFrom || null;
    const to = mode === "date" ? dateFrom : mode === "range" ? dateTo : null;
    setBusy(true);
    try {
      await api("/api/sync/start", {
        method: "POST",
        body: JSON.stringify({ mode: payloadMode, date_from: from, date_to: to }),
      });
      setShowLive(true);
      refresh();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function publishOnline() {
    setError("");
    setMessage("");
    setBusy(true);
    try {
      const result = await api("/api/admin/publish-online", {
        method: "POST",
        body: JSON.stringify({ batch_code: batchCode || null }),
      });
      setMessage(result.message || "Published to online centres.");
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function uploadExcel() {
    setError("");
    setMessage("");
    if (!files.length) {
      setError("Choose one or more Excel files from Kaushal (Download Excel).");
      return;
    }
    if (mode === "date" && !dateFrom) {
      setError("Choose the date to import.");
      return;
    }
    if (mode === "range" && (!dateFrom || !dateTo)) {
      setError("Choose the start date and the end date.");
      return;
    }
    const payloadMode = mode === "date" ? "range" : mode;
    const from = mode === "incremental" ? "" : dateFrom || "";
    const to = mode === "date" ? dateFrom : mode === "range" ? dateTo : "";
    const body = new FormData();
    for (const file of files) body.append("files", file);
    body.append("mode", payloadMode);
    if (from) body.append("date_from", from);
    if (to) body.append("date_to", to);
    setBusy(true);
    try {
      const result = await api("/api/sync/upload-excel", { method: "POST", body });
      setMessage(result.message || "Excel import completed.");
      setFiles([]);
      refresh();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold">Admin portal sync</h1>
        <p className="mt-1 max-w-3xl text-sm leading-6 text-slate-500">
          Only the administrator syncs attendance (Chromium on this Mac). Centre logins view reports online and cannot sync.
          After a local sync, click <span className="font-medium text-slate-700">Publish to online centres</span> so their reports update.
        </p>
        {batchCode && <p className="mt-2 text-sm text-slate-600">Batch on screen: <span className="font-semibold">{batchCode}</span></p>}
      </div>

      {cloudBlocked && (
        <section className="rounded-2xl border border-amber-200 bg-amber-50 p-5 text-sm text-amber-900">
          This online site is for centre reports. Sync Chromium on your Mac at <span className="font-semibold">http://127.0.0.1:8000</span>, then publish here so centres see the updates.
        </section>
      )}

      <section className="rounded-2xl bg-white p-5 shadow-card">
        <h2 className="font-semibold">Chromium sync (administrator)</h2>
        <ol className="mt-3 list-decimal space-y-1 pl-5 text-sm leading-6 text-slate-600">
          <li>On your Mac open the local app and run <span className="font-medium">scripts/open-kaushal-chrome.sh</span></li>
          <li>Sign into Kaushal in that Chrome window</li>
          <li>Start sync below for the batch on screen</li>
          <li>Publish to online so every centre login can see the reports</li>
        </ol>

        <div className="mt-5 flex flex-wrap gap-4 text-sm">
          <label className="flex items-center gap-2"><input type="radio" checked={mode === "incremental"} onChange={() => setMode("incremental")} /> New dates only</label>
          <label className="flex items-center gap-2"><input type="radio" checked={mode === "full"} onChange={() => setMode("full")} /> All dates</label>
          <label className="flex items-center gap-2"><input type="radio" checked={mode === "date"} onChange={() => setMode("date")} /> One date</label>
          <label className="flex items-center gap-2"><input type="radio" checked={mode === "range"} onChange={() => setMode("range")} /> Date range</label>
        </div>
        {mode === "date" && (
          <div className="mt-4">
            <label className="mb-1 block text-xs font-medium text-slate-500">Date</label>
            <input type="date" className="rounded-xl border border-plum-100 px-3 py-2" value={dateFrom} onChange={(event) => setDateFrom(event.target.value)} />
          </div>
        )}
        {mode === "range" && (
          <div className="mt-4 flex flex-wrap gap-3">
            <div>
              <label className="mb-1 block text-xs font-medium text-slate-500">From</label>
              <input type="date" className="rounded-xl border border-plum-100 px-3 py-2" value={dateFrom} onChange={(event) => setDateFrom(event.target.value)} />
            </div>
            <div>
              <label className="mb-1 block text-xs font-medium text-slate-500">To</label>
              <input type="date" className="rounded-xl border border-plum-100 px-3 py-2" value={dateTo} onChange={(event) => setDateTo(event.target.value)} />
            </div>
          </div>
        )}

        <div className="mt-5 flex flex-wrap gap-3">
          <button type="button" disabled={busy || running || cloudBlocked} onClick={startLive} className="rounded-xl bg-plum-700 px-4 py-3 text-sm font-semibold text-white disabled:opacity-50">
            START CHROMIUM SYNC
          </button>
          {running && (
            <button type="button" className="rounded-xl border border-plum-100 px-4 py-3 text-sm font-semibold" onClick={() => api("/api/sync/cancel", { method: "POST" })}>
              Stop sync
            </button>
          )}
          <button type="button" disabled={busy || running} onClick={publishOnline} className="rounded-xl border border-gold px-4 py-3 text-sm font-semibold text-gold disabled:opacity-50">
            PUBLISH TO ONLINE CENTRES
          </button>
          {(current?.status === "awaiting_login" || current?.progress_step === "awaiting_login") && (
            <button type="button" className="rounded-xl border border-plum-100 px-4 py-3 text-sm font-semibold" onClick={() => api("/api/sync/login-done", { method: "POST" })}>
              I have logged in
            </button>
          )}
        </div>
        {completed && !cloudBlocked && (
          <p className="mt-3 text-sm text-emerald-700">Local sync finished. Publish to online so centre logins can see this batch’s reports.</p>
        )}
        {message && <p className="mt-3 text-sm text-emerald-700">{message}</p>}
        {error && <p className="mt-3 text-sm text-red-700">{error}</p>}
      </section>

      {remoteBrowser && (
        <section className="rounded-2xl bg-white p-5 shadow-card">
          <button type="button" className="text-sm font-semibold text-plum-700" onClick={() => setShowLive((value) => !value)}>
            {showLive ? "Hide live window" : "Show live window"}
          </button>
          {showLive && browserSrc ? (
            <iframe title="Kaushal portal live browser" src={browserSrc} className="mt-4 h-[60vh] min-h-[360px] w-full rounded-xl border border-plum-100 bg-slate-900" allow="clipboard-read; clipboard-write" />
          ) : null}
        </section>
      )}

      <section className="rounded-2xl bg-white p-5 shadow-card">
        <button type="button" className="text-sm font-semibold text-slate-600" onClick={() => setShowExcel((value) => !value)}>
          {showExcel ? "Hide emergency Excel import" : "Emergency Excel import (admin fallback)"}
        </button>
        {showExcel && (
          <div className="mt-4 space-y-4">
            <p className="text-sm text-slate-500">Use only if Chromium sync is unavailable. Prefer Chromium + Publish.</p>
            <input
              type="file"
              accept=".xlsx,.xls,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
              multiple
              onChange={(event) => setFiles(Array.from(event.target.files || []))}
              className="block w-full text-sm text-slate-600"
            />
            <button type="button" disabled={busy || running} onClick={uploadExcel} className="rounded-xl border border-plum-100 px-4 py-3 text-sm font-semibold disabled:opacity-50">
              Upload Excel &amp; import
            </button>
          </div>
        )}
      </section>

      <section className="rounded-2xl bg-white p-5 shadow-card">
        <h2 className="font-semibold">Progress</h2>
        <ol className="mt-4 space-y-2">
          {STEPS.map(([key, label], index) => {
            const done = stepIndex > index || current?.status === "completed";
            const active = current?.progress_step === key;
            return (
              <li key={key} className={`rounded-xl px-3 py-2 text-sm ${active ? "bg-plum-700 text-white" : done ? "bg-plum-50 text-plum-800" : "text-slate-400"}`}>
                {label}
                {active && current?.message ? ` · ${current.message}` : ""}
              </li>
            );
          })}
        </ol>
        {current?.error_log && <pre className="mt-4 max-h-48 overflow-auto whitespace-pre-wrap rounded-xl bg-plum-50 p-3 text-xs text-slate-600">{current.error_log}</pre>}
      </section>

      <section className="overflow-auto rounded-2xl bg-white shadow-card">
        <table className="w-full min-w-[720px] text-left text-sm">
          <thead className="text-slate-500">
            <tr>
              {["Started", "Mode", "Status", "Dates", "Records", "Message"].map((heading) => <th key={heading} className="px-4 py-3 font-medium">{heading}</th>)}
            </tr>
          </thead>
          <tbody>
            {history.map((row) => (
              <tr key={row.id} className="border-t border-plum-50">
                <td className="px-4 py-3">{stamp(row.started_at)}</td>
                <td className="px-4 py-3">{row.mode}</td>
                <td className="px-4 py-3 capitalize">{row.status}</td>
                <td className="px-4 py-3">{row.dates_processed}/{row.dates_discovered}</td>
                <td className="px-4 py-3">{row.records_saved}</td>
                <td className="px-4 py-3">{row.message}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
    </div>
  );
}
