import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api } from "../api";

const EMPTY = {
  batch_code: "",
  job_role: "",
  sanction_order: "",
  batch_start_date: "",
  training_start: "09:00",
  training_end: "18:00",
  status: "active",
};

export default function CentreDetail() {
  const { id } = useParams();
  const navigate = useNavigate();
  const [payload, setPayload] = useState(null);
  const [form, setForm] = useState(EMPTY);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [account, setAccount] = useState(null);

  function load() {
    api(`/api/centres/${id}`).then(setPayload).catch((err) => setError(err.message));
  }

  useEffect(() => {
    load();
  }, [id]);

  function update(key, value) {
    setForm((current) => ({ ...current, [key]: value }));
  }

  async function createBatch(event) {
    event.preventDefault();
    setError("");
    setMessage("");
    try {
      const created = await api(`/api/centres/${id}/batches`, { method: "POST", body: JSON.stringify(form) });
      setMessage(`${created.batch.batch_code} was created. Upload its candidate workbook next.`);
      setForm(EMPTY);
      load();
    } catch (err) {
      setError(err.message);
    }
  }

  async function upload(batchCode, event) {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file) return;
    await api("/api/batches/activate", { method: "POST", body: JSON.stringify({ batch_code: batchCode }) });
    const body = new FormData();
    body.append("file", file);
    setError("");
    try {
      const result = await api("/api/candidates/import", { method: "POST", body });
      setMessage(`${result.total_candidates} candidates are stored for ${batchCode}.`);
      load();
    } catch (err) {
      setError(err.message);
    }
  }

  async function syncBatch(batchCode) {
    setError("");
    try {
      await api("/api/batches/activate", { method: "POST", body: JSON.stringify({ batch_code: batchCode }) });
      await api("/api/sync/start", { method: "POST", body: JSON.stringify({ mode: "incremental", date_from: null, date_to: null }) });
      navigate("/sync");
    } catch (err) {
      setError(err.message);
    }
  }

  if (!payload) return <p className="text-sm text-slate-500">{error || "Loading centre…"}</p>;
  const centre = payload.centre;

  return (
    <div className="space-y-6">
      <div>
        <Link className="text-sm text-plum-800" to="/centres">Training centres</Link>
        <h1 className="mt-1 text-2xl font-semibold">{centre.name}</h1>
        <p className="mt-1 text-sm text-slate-500">{centre.code} · {centre.state} · {centre.pia} · {centre.sanction_order}</p>
      </div>
      {payload.role !== "centre" && (
        <section className="rounded-2xl bg-white p-5 shadow-card">
          <h2 className="font-semibold">Centre login</h2>
          <p className="mt-1 text-sm text-slate-500">Create a username and password for this training centre. That login shows only this centre's batches and candidates.</p>
          {payload.login_username && !account && <p className="mt-2 text-sm">Current username: <span className="font-semibold">{payload.login_username}</span>. Creating again replaces the password.</p>}
          <button type="button" className="mt-3 rounded-xl bg-plum-700 px-4 py-2 text-sm font-semibold text-white" onClick={async () => {
            setError("");
            try {
              setAccount(await api(`/api/centres/${id}/login`, { method: "POST" }));
              load();
            } catch (err) {
              setError(err.message);
            }
          }}>Create centre login</button>
          {account && (
            <div className="mt-4 rounded-xl bg-plum-50 p-4 text-sm">
              <p>Give these credentials to {account.centre_name}. The password is shown only here.</p>
              <p className="mt-2">Username: <span className="font-semibold">{account.username}</span></p>
              <p>Password: <span className="font-semibold">{account.password}</span></p>
            </div>
          )}
        </section>
      )}
      {message && <p className="rounded-xl bg-emerald-50 px-4 py-3 text-sm text-emerald-800">{message}</p>}
      {error && <p className="rounded-xl bg-red-50 px-4 py-3 text-sm text-red-800">{error}</p>}

      <form onSubmit={createBatch} className="grid gap-3 rounded-2xl bg-white p-5 shadow-card md:grid-cols-3">
        <h2 className="md:col-span-3 font-semibold">Add batch</h2>
        <Field label="Batch registration number" value={form.batch_code} onChange={(value) => update("batch_code", value)} />
        <Field label="Job role / trade" value={form.job_role} onChange={(value) => update("job_role", value)} />
        <Field label="Sanction order" value={form.sanction_order} onChange={(value) => update("sanction_order", value)} />
        <Field label="Batch start date" type="date" value={form.batch_start_date} onChange={(value) => update("batch_start_date", value)} />
        <Field label="Training start" type="time" value={form.training_start} onChange={(value) => update("training_start", value)} />
        <Field label="Training end" type="time" value={form.training_end} onChange={(value) => update("training_end", value)} />
        <div className="md:col-span-3">
          <button className="rounded-xl bg-plum-700 px-4 py-2 text-sm font-semibold text-white" type="submit">Create batch</button>
        </div>
      </form>

      <div className="grid gap-4 md:grid-cols-2">
        {payload.batches.map((batch) => (
          <article key={batch.id} className="rounded-2xl bg-white p-5 shadow-card">
            <h2 className="text-lg font-semibold">{batch.batch_code}</h2>
            <p className="text-sm text-slate-500">{batch.job_role || "Trade not set"}{batch.batch_start_date ? ` · from ${batch.batch_start_date}` : ""}</p>
            <p className="mt-2 text-sm">{batch.candidate_count} candidates</p>
            <p className="text-sm text-slate-500">{batch.training_start}–{batch.training_end}</p>
            <div className="mt-4 flex flex-wrap gap-2">
              <button type="button" className="rounded-xl border border-plum-100 px-3 py-2 text-sm font-semibold" onClick={async () => { await api("/api/batches/activate", { method: "POST", body: JSON.stringify({ batch_code: batch.batch_code }) }); navigate("/candidates"); }}>View candidates</button>
              <button type="button" className="rounded-xl border border-plum-100 px-3 py-2 text-sm font-semibold" onClick={async () => { await api("/api/batches/activate", { method: "POST", body: JSON.stringify({ batch_code: batch.batch_code }) }); navigate("/matrix"); }}>View attendance</button>
              <button type="button" className="rounded-xl bg-plum-700 px-3 py-2 text-sm font-semibold text-white" onClick={() => syncBatch(batch.batch_code)}>Sync Attendance</button>
            </div>
            <label className="mt-3 block text-sm text-slate-600">
              Upload candidate workbook
              <input className="mt-1 block text-sm" type="file" accept=".xlsx" onChange={(event) => upload(batch.batch_code, event)} />
            </label>
          </article>
        ))}
        {payload.batches.length === 0 && <p className="text-sm text-slate-500">No batch yet for this centre.</p>}
      </div>
    </div>
  );
}

function Field({ label, value, onChange, type = "text" }) {
  return (
    <label className="text-sm text-slate-600">
      {label}
      <input className="mt-1 w-full rounded-xl border border-plum-100 px-3 py-2 text-ink" type={type} value={value ?? ""} onChange={(event) => onChange(event.target.value)} />
    </label>
  );
}
