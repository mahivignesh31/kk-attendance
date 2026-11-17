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
  const [mode, setMode] = useState("date");
  const [dateFrom, setDateFrom] = useState("");
  const [dateTo, setDateTo] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [remoteBrowser, setRemoteBrowser] = useState(false);
  const [remoteBrowserPath, setRemoteBrowserPath] = useState("");

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
  const showLiveBrowser =
    remoteBrowser &&
    running &&
    ["connecting", "awaiting_login", "loading_report", "extracting_records"].includes(current?.progress_step || "");
  const browserSrc = remoteBrowserPath ? apiUrl(remoteBrowserPath) : "";

  async function start() {
    setError("");
    if (mode === "date" && !dateFrom) {
      setError("Choose the date to check.");
      return;
    }
    if (mode === "range" && (!dateFrom || !dateTo)) {
      setError("Choose the start date and the end date.");
      return;
    }
    if (mode === "full" && !window.confirm("Check all dates re-reads every punch date found for this batch. Official portal attendance is never changed. Continue?")) {
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
          {remoteBrowser
            ? "Live only: sign into Kaushal in the window below (normal Chrome). If you see Unauthorized, click Stop sync, start again, and use only the Kaushal login page — then “I have logged in”. Official attendance is never edited."
            : "Sync opens the official Kaushal login on this computer. Sign in yourself, then continue. Official attendance is never edited."}{" "}
          You can check one date, a date range, or all dates.
        </p>
      </div>

      <section className="rounded-2xl bg-white p-5 shadow-card">
        <div className="flex flex-wrap gap-4 text-sm">
          <label className="flex items-center gap-2"><input type="radio" checked={mode === "date"} onChange={() => setMode("date")} /> Check one date</label>
          <label className="flex items-center gap-2"><input type="radio" checked={mode === "range"} onChange={() => setMode("range")} /> Check a date range</label>
          <label className="flex items-center gap-2"><input type="radio" checked={mode === "full"} onChange={() => setMode("full")} /> Check all dates</label>
          <label className="flex items-center gap-2"><input type="radio" checked={mode === "incremental"} onChange={() => setMode("incremental")} /> New dates only</label>
        </div>
        {mode === "date" && (
          <div className="mt-4">
            <label className="mb-1 block text-xs font-medium text-slate-500">Date to check</label>
            <input type="date" aria-label="Date to check" className="rounded-xl border border-plum-100 px-3 py-2" value={dateFrom} onChange={(event) => setDateFrom(event.target.value)} />
          </div>
        )}
        {mode === "range" && (
          <div className="mt-4 flex flex-wrap gap-3">
            <div>
              <label className="mb-1 block text-xs font-medium text-slate-500">From</label>
              <input type="date" aria-label="Range start" className="rounded-xl border border-plum-100 px-3 py-2" value={dateFrom} onChange={(event) => setDateFrom(event.target.value)} />
            </div>
            <div>
              <label className="mb-1 block text-xs font-medium text-slate-500">To</label>
              <input type="date" aria-label="Range end" className="rounded-xl border border-plum-100 px-3 py-2" value={dateTo} onChange={(event) => setDateTo(event.target.value)} />
            </div>
          </div>
        )}
        <div className="mt-5 flex flex-wrap gap-3">
          <button type="button" disabled={busy || running} onClick={start} className="rounded-xl bg-plum-700 px-4 py-3 text-sm font-semibold text-white disabled:opacity-50">
            {mode === "date" ? "CHECK THIS DATE" : mode === "range" ? "CHECK THIS RANGE" : mode === "full" ? "CHECK ALL DATES" : "SYNC NEW DATES"}
          </button>
          {running && (
            <button type="button" className="rounded-xl border border-plum-100 px-4 py-3 text-sm font-semibold" onClick={() => api("/api/sync/cancel", { method: "POST" })}>
              Stop sync
            </button>
          )}
          {remoteBrowser && (
            <button
              type="button"
              className="rounded-xl border border-plum-100 px-4 py-3 text-sm font-semibold"
              onClick={() =>
                api("/api/sync/reopen-browser", { method: "POST" })
                  .then(() => setError(""))
                  .catch((err) => setError(err.message))
              }
            >
              Reload Kaushal login
            </button>
          )}
          {(current?.status === "awaiting_login" || current?.progress_step === "awaiting_login") && (
            <button type="button" className="rounded-xl border border-gold px-4 py-3 text-sm font-semibold text-gold" onClick={() => api("/api/sync/login-done", { method: "POST" })}>
              I have logged in
            </button>
          )}
        </div>
        {error && <p className="mt-3 text-sm text-red-700">{error}</p>}
      </section>

      {remoteBrowser && (
        <section className="rounded-2xl bg-white p-5 shadow-card">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <h2 className="font-semibold">Live Kaushal window</h2>
              <p className="mt-1 text-sm text-slate-500">
                {showLiveBrowser
                  ? "This is a real Chrome on the server. Log into Kaushal here, then click “I have logged in”."
                  : "Live Chrome stays here for Kaushal. Start a sync, sign in in this window, then click “I have logged in”."}
              </p>
            </div>
            {browserSrc && (
              <a href={browserSrc} target="_blank" rel="noreferrer" className="text-sm font-semibold text-plum-700">
                Open in new tab
              </a>
            )}
          </div>
          {browserSrc ? (
            <iframe
              title="Kaushal portal live browser"
              src={browserSrc}
              className="mt-4 h-[70vh] min-h-[420px] w-full rounded-xl border border-plum-100 bg-slate-900"
              allow="clipboard-read; clipboard-write"
            />
          ) : (
            <p className="mt-4 text-sm text-slate-500">Remote browser is not available on this server yet.</p>
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
