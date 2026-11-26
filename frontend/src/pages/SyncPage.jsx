import { useEffect, useMemo, useState } from "react";
import { Navigate } from "react-router-dom";
import { api } from "../api";
import { stamp } from "../utils";

const STEPS = [
  ["connecting", "Opening Chrome"],
  ["awaiting_login", "Waiting for Kaushal login"],
  ["loading_report", "Loading Attendance Report"],
  ["discovering_dates", "Discovering Dates"],
  ["processing_dates", "Processing Dates"],
  ["extracting_records", "Extracting Candidate Records"],
  ["reconciling", "Calculating attendance"],
  ["saving", "Saving Records"],
  ["completed", "Sync Completed"],
  ["cancelling", "Stopping Sync"],
  ["failed", "Sync Failed"],
  ["cancelled", "Sync Cancelled"],
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
  const [publishResult, setPublishResult] = useState(null);
  const [busy, setBusy] = useState(false);
  const [cloudBlocked, setCloudBlocked] = useState(false);
  const [centres, setCentres] = useState([]);
  const [centreId, setCentreId] = useState("");
  const [centreBatches, setCentreBatches] = useState([]);
  const [queue, setQueue] = useState([]);

  const selectedCentre = useMemo(
    () => centres.find((item) => String(item.id) === String(centreId)) || null,
    [centres, centreId]
  );

  const refresh = () => {
    api("/api/sync/current")
      .then((payload) => setCurrent(payload.sync))
      .catch((err) => setError(err.message));
    api("/api/sync/history")
      .then((payload) => setHistory(payload.history))
      .catch(() => {});
    api("/api/admin/publish-queue")
      .then((payload) => setQueue(payload.queue || []))
      .catch(() => {});
  };

  useEffect(() => {
    let active = true;
    (async () => {
      try {
        const [payload, centrePayload] = await Promise.all([
          api("/api/bootstrap"),
          api("/api/centres").catch(() => ({ centres: [] })),
        ]);
        if (!active) return;
        setRole(payload.user?.role || "admin");
        setCloudBlocked(Boolean(payload.cloud_sync_blocked));
        const byId = new Map();
        [...(payload.centres || []), ...(centrePayload.centres || [])].forEach((row) => {
          if (row?.id == null) return;
          byId.set(Number(row.id), { id: row.id, name: row.name, code: row.code });
        });
        const list = Array.from(byId.values()).sort((a, b) => String(a.name).localeCompare(String(b.name)));
        setCentres(list);
        const preferred =
          payload.active_centre_id ??
          list.find((c) => c.name === (payload.active_centre?.name || payload.batch?.training_center))?.id ??
          list[0]?.id;
        if (preferred == null || preferred === "") {
          setCentreId("");
          setCentreBatches([]);
          return;
        }
        const preferredId = String(preferred);
        setCentreId(preferredId);
        setCentreBatches(
          (payload.batches || []).filter((row) => String(row.centre_id) === preferredId)
        );
        try {
          const result = await api("/api/centres/activate", {
            method: "POST",
            body: JSON.stringify({ centre_id: Number(preferredId) }),
          });
          if (!active) return;
          if (Array.isArray(result.batches)) setCentreBatches(result.batches);
        } catch (err) {
          if (active) setError(err.message || "Could not load centre batches.");
        }
      } catch {
        if (active) setRole("admin");
      }
    })();
    refresh();
    const timer = setInterval(refresh, 500);
    return () => {
      active = false;
      clearInterval(timer);
    };
  }, []);

  if (role === "centre") {
    return <Navigate to="/" replace />;
  }

  const running = ["running", "awaiting_login", "cancelling"].includes(current?.status);
  const completed = current?.status === "completed";
  const failed = current?.status === "failed";
  const cancelled = current?.status === "cancelled";
  const stepKey = current?.progress_step || current?.status || "";
  const stepIndex = STEPS.findIndex((step) => step[0] === stepKey);

  const bannerTone =
    failed || cancelled
      ? "bg-red-700"
      : completed
        ? "bg-emerald-700"
        : running
          ? "bg-plum-700"
          : "bg-slate-700";

  async function selectCentre(nextId) {
    setError("");
    setMessage("");
    setPublishResult(null);
    if (!nextId) return;
    setBusy(true);
    try {
      const result = await api("/api/centres/activate", {
        method: "POST",
        body: JSON.stringify({ centre_id: Number(nextId) }),
      });
      setCentreId(String(nextId));
      setCentreBatches(result.batches || []);
      setMessage(`${result.centre?.name || "Centre"} selected · ${(result.batches || []).length} batch(es).`);
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
    setPublishResult(null);
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
          ? `Sync started for all centres (${count} batches). Sign into Kaushal in Chrome — sync continues automatically.`
          : `Sync started for ${selectedCentre?.name || "centre"} · ${count} batch(es). Sign into Kaushal in Chrome — sync continues automatically.`
      );
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
    setPublishResult(null);
    setBusy(true);
    try {
      const result = await api("/api/admin/publish-online", {
        method: "POST",
        body: JSON.stringify({ all_centres: true }),
      });
      setPublishResult(result);
      setMessage(result.message || "Published to online centres.");
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
        <h1 className="text-2xl font-semibold">Sync attendance</h1>
        <p className="mt-1 max-w-3xl text-sm leading-6 text-slate-500">
          Select a centre, sync from Kaushal in Chrome, then publish so every centre login can view reports online.
        </p>
      </div>

      <section className="rounded-2xl bg-white p-5 shadow-card">
        <h2 className="font-semibold">Training centre</h2>
        <label className="mt-4 block text-sm text-slate-600">
          Centre
          <select
            name="kk-training-centre"
            autoComplete="off"
            className="mt-1 w-full max-w-md rounded-xl border border-plum-100 px-3 py-2 text-ink"
            value={centreId}
            onChange={(event) => selectCentre(event.target.value)}
            disabled={busy || running || centres.length === 0}
          >
            {centres.map((centre) => (
              <option key={centre.id} value={String(centre.id)}>
                {centre.name}
              </option>
            ))}
          </select>
        </label>
        {centres.length === 0 && (
          <p className="mt-2 text-sm text-amber-800">Loading centres… If this stays empty, open Training Centres and refresh.</p>
        )}

        <div className="mt-4 space-y-2">
          {centreBatches.map((item) => {
            const code = item.batch_code;
            const records = queue.find((row) => row.batch_code === code)?.record_count;
            return (
              <div
                key={code}
                className="flex flex-wrap items-center justify-between gap-2 rounded-xl bg-plum-50 px-3 py-2 text-sm text-slate-700"
              >
                <div>
                  <span className="font-semibold">{item.training_center || selectedCentre?.name || "Training centre"}</span>
                  <span className="text-slate-500">
                    {" "}
                    · {code} · {item.candidate_count != null ? item.candidate_count : "—"} candidates
                    {records != null ? ` · ${records} records` : ""}
                  </span>
                </div>
              </div>
            );
          })}
          {centreId && centreBatches.length === 0 && (
            <p className="text-sm text-slate-500">This centre has no batches yet.</p>
          )}
        </div>
      </section>

      {cloudBlocked && (
        <section className="rounded-2xl border border-amber-200 bg-amber-50 p-5 text-sm text-amber-900">
          Online site is view-only. Run sync on the administrator Mac at http://127.0.0.1:8000, then publish.
        </section>
      )}

      <section className="rounded-2xl bg-white p-5 shadow-card">
        <h2 className="font-semibold">Sync</h2>
        <ol className="mt-3 list-decimal space-y-1 pl-5 text-sm leading-6 text-slate-600">
          <li>Click Sync — Chrome opens automatically</li>
          <li>Sign into Kaushal in that Chrome window (sync continues by itself)</li>
          <li>Watch Progress for each candidate</li>
          <li>When finished, click Publish all centres</li>
        </ol>

        <p className="mt-4 text-sm text-slate-500">Syncs through today (IST). Future dates are skipped. Today is included again on the next sync so later punches are picked up.</p>
        <div className="mt-3 flex flex-wrap gap-4 text-sm">
          <label className="flex items-center gap-2">
            <input type="radio" checked={mode === "incremental"} onChange={() => setMode("incremental")} /> New dates only
          </label>
          <label className="flex items-center gap-2">
            <input type="radio" checked={mode === "full"} onChange={() => setMode("full")} /> All dates
          </label>
          <label className="flex items-center gap-2">
            <input type="radio" checked={mode === "date"} onChange={() => setMode("date")} /> One date
          </label>
          <label className="flex items-center gap-2">
            <input type="radio" checked={mode === "range"} onChange={() => setMode("range")} /> Date range
          </label>
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
          <button
            type="button"
            disabled={busy || running || cloudBlocked || !centreId}
            onClick={() => startLive({ allCentres: false })}
            className="rounded-xl bg-plum-700 px-4 py-3 text-sm font-semibold text-white disabled:opacity-50"
          >
            SYNC THIS CENTRE
          </button>
          <button
            type="button"
            disabled={busy || running || cloudBlocked}
            onClick={() => startLive({ allCentres: true })}
            className="rounded-xl border border-plum-100 px-4 py-3 text-sm font-semibold disabled:opacity-50"
          >
            Sync every centre
          </button>
          {running && (
            <button
              type="button"
              className="rounded-xl border border-red-200 px-4 py-3 text-sm font-semibold text-red-700"
              onClick={() => api("/api/sync/cancel", { method: "POST" }).then(refresh)}
            >
              Stop sync
            </button>
          )}
        </div>
        {message && <p className="mt-3 text-sm text-emerald-700">{message}</p>}
        {error && <p className="mt-3 text-sm text-red-700">{error}</p>}
      </section>

      <section className={`rounded-2xl p-5 shadow-card text-white ${bannerTone}`}>
        <h2 className="font-semibold">Live progress</h2>
        <div className="mt-2 text-sm font-semibold capitalize">
          {current ? `${current.status} · ${current.progress_step || "—"}` : "Idle"}
        </div>
        <div className="mt-2 text-base leading-6">{current?.message || "Click Sync this centre to begin."}</div>
        <div className="mt-2 text-sm opacity-90">
          {current
            ? `Records saved: ${current.records_saved || 0} · candidate ${current.progress_current || 0} / ${current.progress_total || 0}`
            : ""}
        </div>
      </section>

      <section className="rounded-2xl bg-white p-5 shadow-card">
        <h2 className="font-semibold">Steps</h2>
        <ol className="mt-4 space-y-2">
          {STEPS.filter(([key]) => !["failed", "cancelled"].includes(key) || stepKey === key).map(([key, label], index) => {
            const done = stepIndex > index || current?.status === "completed";
            const active = stepKey === key;
            return (
              <li
                key={key}
                className={`rounded-xl px-3 py-2 text-sm ${active ? "bg-plum-700 text-white" : done ? "bg-plum-50 text-plum-800" : "text-slate-400"}`}
              >
                {label}
              </li>
            );
          })}
        </ol>
        {current?.error_log && (
          <pre className="mt-4 max-h-48 overflow-auto whitespace-pre-wrap rounded-xl bg-plum-50 p-3 text-xs text-slate-600">{current.error_log}</pre>
        )}
      </section>

      {(completed || (current?.records_saved > 0 && (failed || cancelled))) && !cloudBlocked && (
        <section className="rounded-2xl border-2 border-gold bg-white p-5 shadow-card">
          <h2 className="font-semibold text-ink">Publish to centres</h2>
          <p className="mt-2 text-sm text-slate-600">
            Local sync has data. Publish so every centre login can see attendance online.
            {current?.records_saved != null ? ` This run saved ${current.records_saved} records.` : ""}
          </p>
          <button
            type="button"
            disabled={busy || running}
            onClick={publishOnline}
            className="mt-4 rounded-xl bg-gold px-4 py-3 text-sm font-semibold text-white disabled:opacity-50"
          >
            PUBLISH ALL CENTRES
          </button>
          {publishResult?.published && (
            <ul className="mt-4 space-y-1 text-sm text-slate-700">
              {publishResult.published.map((row) => (
                <li key={`${row.centre_name}-${row.batch_code}`}>
                  {row.centre_name || "Centre"} · {row.batch_code}: {row.records ?? "—"} records
                  {row.message ? ` — ${row.message}` : ""}
                </li>
              ))}
            </ul>
          )}
        </section>
      )}

      {!completed && !failed && !cancelled && !cloudBlocked && (
        <section className="rounded-2xl bg-white p-5 shadow-card">
          <button type="button" disabled={busy || running} onClick={publishOnline} className="rounded-xl border border-gold px-4 py-3 text-sm font-semibold text-gold disabled:opacity-50">
            PUBLISH ALL CENTRES
          </button>
          <p className="mt-2 text-xs text-slate-500">Use after a successful sync so online centres see updates.</p>
        </section>
      )}

      <section className="overflow-auto rounded-2xl bg-white shadow-card">
        <table className="w-full min-w-[720px] text-left text-sm">
          <thead className="text-slate-500">
            <tr>
              {["Started", "Mode", "Status", "Dates", "Records", "Message"].map((heading) => (
                <th key={heading} className="px-4 py-3 font-medium">
                  {heading}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {history.map((row) => (
              <tr key={row.id} className="border-t border-plum-50">
                <td className="px-4 py-3">{stamp(row.started_at)}</td>
                <td className="px-4 py-3">{row.mode}</td>
                <td className="px-4 py-3 capitalize">{row.status}</td>
                <td className="px-4 py-3">
                  {row.dates_processed}/{row.dates_discovered}
                </td>
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
