import { useEffect, useState } from "react";
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
  const [current, setCurrent] = useState(null);
  const [history, setHistory] = useState([]);
  const [mode, setMode] = useState("full");
  const [dateFrom, setDateFrom] = useState("");
  const [dateTo, setDateTo] = useState("");
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [files, setFiles] = useState([]);
  const [remoteBrowser, setRemoteBrowser] = useState(false);
  const [remoteBrowserPath, setRemoteBrowserPath] = useState("");
  const [showLive, setShowLive] = useState(false);

  const refresh = () => {
    api("/api/sync/current").then((payload) => setCurrent(payload.sync)).catch((err) => setError(err.message));
    api("/api/sync/history").then((payload) => setHistory(payload.history)).catch(() => {});
  };

  useEffect(() => {
    api("/api/bootstrap")
      .then((payload) => {
        setRemoteBrowser(Boolean(payload.remote_browser));
        setRemoteBrowserPath(payload.remote_browser_path || "");
      })
      .catch(() => {});
    refresh();
    const timer = setInterval(refresh, 1500);
    return () => clearInterval(timer);
  }, []);

  const running = ["running", "awaiting_login", "cancelling"].includes(current?.status);
  const stepIndex = STEPS.findIndex((step) => step[0] === current?.progress_step);
  const browserSrc = remoteBrowserPath ? apiUrl(remoteBrowserPath) : "";

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

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold">Portal sync</h1>
        <p className="mt-1 max-w-3xl text-sm leading-6 text-slate-500">
          Upload Kaushal <span className="font-medium text-slate-700">Download Excel</span> files here (works on the online site).
          Official attendance is never edited.
        </p>
      </div>

      <section className="rounded-2xl bg-white p-5 shadow-card">
        <h2 className="font-semibold">Upload Kaushal Excel</h2>
        <ol className="mt-3 list-decimal space-y-1 pl-5 text-sm leading-6 text-slate-600">
          <li>Open Kaushal in your normal browser and sign in</li>
          <li>Go to Candidate Attendance Report → enter Candidate ID → Submit</li>
          <li>Click <span className="font-medium">Download Excel</span> for each candidate</li>
          <li>Select <span className="font-medium">all Excel files at once</span> below and import</li>
        </ol>
        <p className="mt-3 text-sm text-slate-500">
          Dates before this batch’s start date are ignored automatically.
        </p>

        <div className="mt-5 flex flex-wrap gap-4 text-sm">
          <label className="flex items-center gap-2"><input type="radio" checked={mode === "full"} onChange={() => setMode("full")} /> All dates in file</label>
          <label className="flex items-center gap-2"><input type="radio" checked={mode === "date"} onChange={() => setMode("date")} /> One date</label>
          <label className="flex items-center gap-2"><input type="radio" checked={mode === "range"} onChange={() => setMode("range")} /> Date range</label>
          <label className="flex items-center gap-2"><input type="radio" checked={mode === "incremental"} onChange={() => setMode("incremental")} /> New dates only</label>
        </div>
        {mode === "date" && (
          <div className="mt-4">
            <label className="mb-1 block text-xs font-medium text-slate-500">Date to import</label>
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

        <div className="mt-5">
          <input
            type="file"
            accept=".xlsx,.xls,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            multiple
            onChange={(event) => setFiles(Array.from(event.target.files || []))}
            className="block w-full text-sm text-slate-600"
          />
          {files.length > 0 && (
            <p className="mt-2 text-xs text-slate-500">{files.length} file(s) selected</p>
          )}
        </div>

        <div className="mt-5 flex flex-wrap gap-3">
          <button type="button" disabled={busy || running} onClick={uploadExcel} className="rounded-xl bg-plum-700 px-4 py-3 text-sm font-semibold text-white disabled:opacity-50">
            UPLOAD EXCEL &amp; IMPORT
          </button>
        </div>
        {message && <p className="mt-3 text-sm text-emerald-700">{message}</p>}
        {error && <p className="mt-3 text-sm text-red-700">{error}</p>}
      </section>

      {remoteBrowser && (
        <section className="rounded-2xl bg-white p-5 shadow-card">
          <button type="button" className="text-sm font-semibold text-plum-700" onClick={() => setShowLive((value) => !value)}>
            {showLive ? "Hide live window (often blocked by Kaushal)" : "Show live window (optional — often returns 401)"}
          </button>
          {showLive && (
            <div className="mt-4">
              <div className="mb-4 flex flex-wrap gap-3">
                <button type="button" disabled={busy || running} onClick={startLive} className="rounded-xl border border-plum-100 px-4 py-3 text-sm font-semibold disabled:opacity-50">
                  Start live sync
                </button>
                {running && (
                  <button type="button" className="rounded-xl border border-plum-100 px-4 py-3 text-sm font-semibold" onClick={() => api("/api/sync/cancel", { method: "POST" })}>
                    Stop sync
                  </button>
                )}
                <button type="button" className="rounded-xl border border-plum-100 px-4 py-3 text-sm font-semibold" onClick={() => api("/api/sync/reopen-browser", { method: "POST" }).catch((err) => setError(err.message))}>
                  Reload Kaushal login
                </button>
                {(current?.status === "awaiting_login" || current?.progress_step === "awaiting_login") && (
                  <button type="button" className="rounded-xl border border-gold px-4 py-3 text-sm font-semibold text-gold" onClick={() => api("/api/sync/login-done", { method: "POST" })}>
                    I have logged in
                  </button>
                )}
              </div>
              {browserSrc ? (
                <iframe title="Kaushal portal live browser" src={browserSrc} className="h-[60vh] min-h-[360px] w-full rounded-xl border border-plum-100 bg-slate-900" allow="clipboard-read; clipboard-write" />
              ) : null}
            </div>
          )}
        </section>
      )}

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
