import { useEffect, useState } from "react";
import { api } from "../api";

const RESET_PHRASE = "RESET LOCAL ATTENDANCE";

export default function Settings() {
  const [form, setForm] = useState(null);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [phrase, setPhrase] = useState("");

  useEffect(() => {
    api("/api/settings").then(setForm).catch((err) => setError(err.message));
  }, []);

  function update(section, key, value) {
    setForm((current) => ({ ...current, [section]: { ...current[section], [key]: value } }));
  }

  async function save(event) {
    event.preventDefault();
    setError("");
    setMessage("");
    try {
      const saved = await api("/api/settings", { method: "PUT", body: JSON.stringify(form) });
      setForm(saved);
      setMessage("Settings saved. Local hour statuses were recalculated from stored check-in and check-out times.");
    } catch (err) {
      setError(err.message);
    }
  }

  async function reset() {
    setError("");
    try {
      await api("/api/admin/reset", { method: "POST", body: JSON.stringify({ confirmation: phrase }) });
      setPhrase("");
      setMessage("Ongole centre data was cleared. Create a batch and upload its master workbook. The Kaushal portal was not changed.");
    } catch (err) {
      setError(err.message);
    }
  }

  if (!form) return <p className="text-sm text-slate-500">{error || "Loading settings…"}</p>;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold">Administrator controls</h1>
        <p className="mt-1 max-w-3xl text-sm text-slate-500">Ongole can hold as many batches as you create. Each batch is the master workbook for that batch. Candidates are matched by KP Id only, then by exact name. Attendance Id is not used.</p>
      </div>
      {message && <p className="rounded-xl bg-emerald-50 px-4 py-3 text-sm text-emerald-800">{message}</p>}
      {error && <p className="rounded-xl bg-red-50 px-4 py-3 text-sm text-red-800">{error}</p>}

      <form onSubmit={save} className="space-y-6">
        <section className="rounded-2xl bg-white p-5 shadow-card">
          <h2 className="font-semibold">Centre</h2>
          <p className="mt-1 text-sm text-slate-500">These belong to KKERS-Ongole. They stay the same for every batch. Change the batch from the list below, or from the Batch on screen menu at the top.</p>
          <div className="mt-4 grid gap-3 md:grid-cols-2">
            <Field label="State" value={form.batch.state} onChange={(value) => update("batch", "state", value)} />
            <Field label="PIA" value={form.batch.pia} onChange={(value) => update("batch", "pia", value)} />
            <Field label="Sanction order" value={form.batch.sanction_order} onChange={(value) => update("batch", "sanction_order", value)} />
            <Field label="Training center" value={form.batch.training_center} onChange={(value) => update("batch", "training_center", value)} />
          </div>
        </section>
        <section className="rounded-2xl bg-white p-5 shadow-card">
          <h2 className="font-semibold">Attendance day status (SOP)</h2>
          <p className="mt-1 text-sm leading-6 text-slate-500">
            Classroom only (RF / OJT not counted). Schedule {form.rules.training_start}–{form.rules.training_end} ({form.rules.scheduled_hours} hrs). Lunch is not subtracted.
            Late or early leave: 0–15 min = Full day (condoned); 16–60 min = Full day + 1 grace; 61–240 min = Half day; more than 4 hrs = Absent.
            A check-in without a check-out is absent. Out time 00:00:00 is a missing check-out. No punch on a day class was held = Absent.
            Grace: 2 occasions in any 15 calendar days; a further occasion becomes Half day.
            Classroom % = (full days + half days × 0.5) / days class was held × 100. Alert below {form.rules.low_attendance_threshold}%.
          </p>
          <div className="mt-4 grid gap-3 md:grid-cols-3">
            <Field label="Training start" type="time" value={form.rules.training_start} onChange={(value) => update("rules", "training_start", value)} />
            <Field label="Training end" type="time" value={form.rules.training_end} onChange={(value) => update("rules", "training_end", value)} />
            <Field label="Low attendance below (%)" type="number" value={form.rules.low_attendance_threshold} onChange={(value) => update("rules", "low_attendance_threshold", Number(value))} />
          </div>
          <p className="mt-4 text-sm text-slate-600">Attendance uses the days this batch actually held class. Sundays are left out. A weekday with no class is not counted as absent.</p>
          <label className="mt-2 flex items-center gap-2 text-sm"><input type="checkbox" checked={!!form.rules.count_time_outside_schedule} onChange={(event) => update("rules", "count_time_outside_schedule", event.target.checked)} /> Count time outside the scheduled 9:00–6:00 window</label>
          <button className="mt-5 rounded-xl bg-plum-700 px-4 py-2 text-sm font-semibold text-white" type="submit">Save settings</button>
        </section>
      </form>

      <BatchSection onMessage={setMessage} onError={setError} />

      <section className="rounded-2xl border border-red-200 bg-white p-5">
        <h2 className="font-semibold text-red-800">Reset Ongole centre</h2>
        <p className="mt-1 text-sm text-slate-500">This clears local candidates, dates and punches for the centre so you can create batches again. It does not change the Kaushal portal. Type {RESET_PHRASE} to continue.</p>
        <div className="mt-3 flex flex-wrap gap-2">
          <input className="rounded-xl border border-red-200 px-3 py-2 text-sm" value={phrase} onChange={(event) => setPhrase(event.target.value)} />
          <button type="button" disabled={phrase !== RESET_PHRASE} className="rounded-xl bg-red-700 px-4 py-2 text-sm font-semibold text-white disabled:opacity-40" onClick={reset}>Reset local data</button>
        </div>
      </section>
    </div>
  );
}

function BatchSection({ onMessage, onError }) {
  const [batches, setBatches] = useState([]);
  const [active, setActive] = useState("");
  const [code, setCode] = useState("");
  const [start, setStart] = useState("");

  function load() {
    api("/api/batches").then((payload) => {
      setBatches(payload.batches || []);
      setActive(payload.active_batch_code || "");
    }).catch((err) => onError(err.message));
  }

  useEffect(() => {
    load();
  }, []);

  async function activate(batchCode) {
    onError("");
    onMessage("");
    try {
      await api("/api/batches/activate", { method: "POST", body: JSON.stringify({ batch_code: batchCode }) });
      setActive(batchCode);
      onMessage(`${batchCode} is the batch on screen.`);
    } catch (err) {
      onError(err.message);
    }
  }

  async function create(event) {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file || !code || !start) {
      onError("Enter the batch code and start date, then choose the master workbook.");
      return;
    }
    const body = new FormData();
    body.append("batch_code", code);
    body.append("batch_start_date", start);
    body.append("file", file);
    onError("");
    onMessage("");
    try {
      const result = await api("/api/batches", { method: "POST", body });
      onMessage(`${result.batch_code} created with ${result.total_candidates} candidates. Matched later by KP Id and name.`);
      setCode("");
      load();
    } catch (err) {
      onError(err.message);
    }
  }

  return (
    <section className="rounded-2xl bg-white p-5 shadow-card">
      <h2 className="font-semibold">Batches at KKERS-Ongole</h2>
      <p className="mt-1 text-sm text-slate-500">Choose which batch the dashboard, candidates, and sync should show. Upload another master workbook to add a batch.</p>
      <ul className="mt-4 space-y-2">
        {batches.map((batch) => (
          <li key={batch.batch_code} className="flex flex-wrap items-center justify-between gap-2 rounded-xl bg-plum-50 px-3 py-2 text-sm">
            <span>{batch.batch_code} · {batch.candidate_count} candidates{batch.batch_start_date ? ` · from ${batch.batch_start_date}` : ""}</span>
            {batch.batch_code === active ? (
              <span className="font-semibold text-plum-800">On screen</span>
            ) : (
              <button type="button" className="rounded-lg bg-plum-700 px-3 py-1.5 font-semibold text-white" onClick={() => activate(batch.batch_code)}>Show this batch</button>
            )}
          </li>
        ))}
      </ul>
      <div className="mt-4 grid gap-3 md:grid-cols-3">
        <Field label="New batch code" value={code} onChange={setCode} />
        <Field label="Batch start date" type="date" value={start} onChange={setStart} />
      </div>
      <label className="mt-3 block text-sm text-slate-600">
        Master workbook
        <input className="mt-1 block text-sm" type="file" accept=".xlsx" onChange={create} />
      </label>
    </section>
  );
}

function Field({ label, value, onChange, type = "text" }) {
  return (
    <label className="text-sm text-slate-600">
      {label}
      <input className="mt-1 w-full rounded-xl border border-plum-100 px-3 py-2 text-ink" type={type} step={type === "number" ? "0.5" : undefined} value={value ?? ""} onChange={(event) => onChange(event.target.value)} />
    </label>
  );
}
