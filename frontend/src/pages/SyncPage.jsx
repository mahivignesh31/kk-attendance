import { useEffect, useMemo, useState } from "react";
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
  const [centres, setCentres] = useState([]);
  const [centreId, setCentreId] = useState("");
  const [centreBatches, setCentreBatches] = useState([]);
  const [queue, setQueue] = useState([]);
  const [remoteBrowser, setRemoteBrowser] = useState(false);
  const [remoteBrowserPath, setRemoteBrowserPath] = useState("");
  const [showLive, setShowLive] = useState(true);

  const selectedCentre = useMemo(
    () => centres.find((item) => String(item.id) === String(centreId)) || null,
    [centres, centreId]
  );

  const refresh = () => {
    api("/api/sync/current").then((payload) => setCurrent(payload.sync)).catch((err) => setError(err.message));
    api("/api/sync/history").then((payload) => setHistory(payload.history)).catch(() => {});
    api("/api/admin/publish-queue")
      .then((payload) => setQueue(payload.queue || []))
      .catch(() => {});
  };

  const loadCentreScope = async (id) => {
    const boot = await api("/api/bootstrap");
    setCentres(boot.centres || []);
    const nextId = id || boot.active_centre_id || boot.centres?.[0]?.id || "";
    setCentreId(nextId ? String(nextId) : "");
    setCentreBatches(boot.batches || []);
    setCloudBlocked(Boolean(boot.cloud_sync_blocked));
    setRemoteBrowser(Boolean(boot.remote_browser));
    setRemoteBrowserPath(boot.remote_browser_path || "");
    return boot;
  };

  useEffect(() => {
    api("/api/bootstrap")
      .then(async (payload) => {
        setRole(payload.user?.role || "admin");
        setCloudBlocked(Boolean(payload.cloud_sync_blocked));
        setRemoteBrowser(Boolean(payload.remote_browser));
        setRemoteBrowserPath(payload.remote_browser_path || "");
        setCentres(payload.centres || []);
        const nextId = payload.active_centre_id || payload.centres?.[0]?.id || "";
        setCentreId(nextId ? String(nextId) : "");
        setCentreBatches(payload.batches || []);
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

  async function selectCentre(nextId) {
    setError("");
    setMessage("");
    if (!nextId) return;
    setBusy(true);
    try {
      const result = await api("/api/centres/activate", {
        method: "POST",
        body: JSON.stringify({ centre_id: Number(nextId) }),
      });
      setCentreId(String(nextId));
      setCentreBatches(result.batches || []);
      setMessage(
        `${result.centre?.name || "Centre"} selected · ${(result.batches || []).length} batch(es). One sync reads every batch.`
      );
      await loadCentreScope(nextId);
      refresh();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function startLive({ allCentres = false } = {}) {
    setError("");
    setMessage("");
    if (!allCentres && !centreId) {
      setError("Select a training centre first.");
      return;
    }
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
      const result = await api("/api/sync/start", {
        method: "POST",
        body: JSON.stringify({
          mode: payloadMode,
          date_from: from,
          date_to: to,
          centre_id: allCentres ? null : Number(centreId),
          all_centres: allCentres,
        }),
      });
      const count = result.batch_count || result.batch_codes?.length || 0;
      setMessage(
        allCentres
          ? `Sync started for all centres (${count} batches).`
          : `Sync started for ${selectedCentre?.name || "centre"} · ${count} batch(es).`
      );
      setShowLive(true);
      refresh();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function publishOnline(allCentres = false) {
    setError("");
    setMessage("");
    setBusy(true);
    try {
      const result = await api("/api/admin/publish-online", {
        method: "POST",
        body: JSON.stringify(
          allCentres
            ? { all_centres: true }
            : { batch_code: centreBatches[0]?.batch_code || null, all_centres: false }
        ),
      });
      setMessage(result.message || "Published to online centres.");
      refresh();
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

  const centreQueue = queue.filter((item) => String(item.centre_id) === String(centreId));

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold">Admin portal sync</h1>
        <p className="mt-1 max-w-3xl text-sm leading-6 text-slate-500">
          Select a <span className="font-medium text-slate-700">training centre</span>, then sync{" "}
          <span className="font-medium text-slate-700">all of its batches</span> with one button. After syncing, publish online so centre logins can see reports.
        </p>
      </div>

      <section className="rounded-2xl bg-white p-5 shadow-card">
        <h2 className="font-semibold">Training centre</h2>
        <p className="mt-1 text-sm text-slate-500">Pick the centre only. Every batch under it is listed below and synced together.</p>
        <label className="mt-4 block text-sm text-slate-600">
          Centre
          <select
            className="mt-1 w-full max-w-md rounded-xl border border-plum-100 px-3 py-2 text-ink"
            value={centreId}
            onChange={(event) => selectCentre(event.target.value)}
            disabled={busy || running}
          >
            <option value="">Select training centre</option>
            {centres.map((centre) => (
              <option key={centre.id} value={centre.id}>
                {centre.name}
              </option>
            ))}
          </select>
        </label>

        <div className="mt-4 space-y-2">
          {(centreBatches.length ? centreBatches : centreQueue).map((item) => {
            const code = item.batch_code;
            const records = queue.find((row) => row.batch_code === code)?.record_count;
            return (
              <div key={code} className="flex flex-wrap items-center justify-between gap-2 rounded-xl bg-plum-50 px-3 py-2 text-sm text-slate-700">
                <div>
                  <span className="font-semibold">{selectedCentre?.name || item.training_center || item.centre_name || "Centre"}</span>
                  <span className="text-slate-500">
                    {" "}
                    · {code} · {item.candidate_count || 0} candidates
                    {records != null ? ` · ${records} records` : ""}
                  </span>
                </div>
              </div>
            );
          })}
          {centreId && centreBatches.length === 0 && centreQueue.length === 0 && (
            <p className="text-sm text-slate-500">This centre has no batches yet.</p>
          )}
          {!centreId && <p className="text-sm text-slate-500">Select a training centre to see its batches.</p>}
        </div>
      </section>

      {cloudBlocked && (
        <section className="rounded-2xl border border-amber-200 bg-amber-50 p-5 text-sm text-amber-900">
          This online site is for centre reports. Sync Chromium on your Mac at <span className="font-semibold">http://127.0.0.1:8000</span>, then publish here so centres see the updates.
        </section>
      )}

      <section className="rounded-2xl bg-white p-5 shadow-card">
        <h2 className="font-semibold">Chromium sync (administrator)</h2>
        <ol className="mt-3 list-decimal space-y-1 pl-5 text-sm leading-6 text-slate-600">
          <li>Run <span className="font-medium">scripts/open-kaushal-chrome.sh</span> and sign into Kaushal</li>
          <li>Select the training centre above</li>
          <li>Click <span className="font-medium">Sync all batches for this centre</span></li>
          <li>When finished, click <span className="font-medium">Publish all centres</span></li>
        </ol>

        <p className="mt-4 text-sm text-slate-500">Sync covers completed days only — through yesterday (IST). Today is never synced.</p>
        <div className="mt-3 flex flex-wrap gap-4 text-sm">
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
          <button type="button" disabled={busy || running || cloudBlocked || !centreId} onClick={() => startLive({ allCentres: false })} className="rounded-xl bg-plum-700 px-4 py-3 text-sm font-semibold text-white disabled:opacity-50">
            SYNC ALL BATCHES FOR THIS CENTRE
          </button>
          <button type="button" disabled={busy || running || cloudBlocked} onClick={() => startLive({ allCentres: true })} className="rounded-xl border border-plum-100 px-4 py-3 text-sm font-semibold disabled:opacity-50">
            Sync every centre
          </button>
          {running && (
            <button type="button" className="rounded-xl border border-plum-100 px-4 py-3 text-sm font-semibold" onClick={() => api("/api/sync/cancel", { method: "POST" })}>
              Stop sync
            </button>
          )}
          <button type="button" disabled={busy || running} onClick={() => publishOnline(true)} className="rounded-xl border border-gold px-4 py-3 text-sm font-semibold text-gold disabled:opacity-50">
            PUBLISH ALL CENTRES
          </button>
          {(current?.status === "awaiting_login" || current?.progress_step === "awaiting_login") && (
            <button type="button" className="rounded-xl border border-plum-100 px-4 py-3 text-sm font-semibold" onClick={() => api("/api/sync/login-done", { method: "POST" })}>
              I have logged in
            </button>
          )}
        </div>
        {completed && !cloudBlocked && (
          <p className="mt-3 text-sm text-emerald-700">Local sync finished. Publish online so centre logins can see the reports.</p>
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
