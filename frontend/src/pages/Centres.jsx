import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";

const EMPTY = {
  name: "",
  code: "",
  state: "ANDHRA PRADESH",
  district: "",
  address: "",
  pia: "",
  sanction_order: "",
  status: "active",
};

export default function Centres() {
  const [centres, setCentres] = useState([]);
  const [role, setRole] = useState("admin");
  const [form, setForm] = useState(EMPTY);
  const [editing, setEditing] = useState(null);
  const [error, setError] = useState("");
  const [open, setOpen] = useState(false);

  function load() {
    api("/api/centres").then((payload) => {
      setCentres(payload.centres);
      setRole(payload.role || "admin");
    }).catch((err) => setError(err.message));
  }

  useEffect(() => {
    load();
  }, []);

  function update(key, value) {
    setForm((current) => ({ ...current, [key]: value }));
  }

  async function save(event) {
    event.preventDefault();
    setError("");
    try {
      if (editing) {
        await api(`/api/centres/${editing}`, { method: "PUT", body: JSON.stringify(form) });
      } else {
        await api("/api/centres", { method: "POST", body: JSON.stringify(form) });
      }
      setForm(EMPTY);
      setEditing(null);
      setOpen(false);
      load();
    } catch (err) {
      setError(err.message);
    }
  }

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold">Training centres</h1>
          <p className="mt-1 text-sm text-slate-500">Create a centre once. Batches and candidate lists belong to that centre.</p>
        </div>
        {role !== "centre" && (
          <button type="button" className="rounded-xl bg-plum-700 px-4 py-2 text-sm font-semibold text-white" onClick={() => { setEditing(null); setForm(EMPTY); setOpen(true); }}>
            Add training centre
          </button>
        )}
      </div>
      {error && <p className="rounded-xl bg-red-50 px-4 py-3 text-sm text-red-800">{error}</p>}
      {open && (
        <form onSubmit={save} className="grid gap-3 rounded-2xl bg-white p-5 shadow-card md:grid-cols-2">
          <Field label="Training centre name" value={form.name} onChange={(value) => update("name", value)} />
          <Field label="Training centre code" value={form.code} onChange={(value) => update("code", value)} />
          <Field label="State" value={form.state} onChange={(value) => update("state", value)} />
          <Field label="District" value={form.district} onChange={(value) => update("district", value)} />
          <Field label="Address" value={form.address} onChange={(value) => update("address", value)} />
          <Field label="PIA name" value={form.pia} onChange={(value) => update("pia", value)} />
          <Field label="Sanction order" value={form.sanction_order} onChange={(value) => update("sanction_order", value)} />
          <label className="text-sm text-slate-600">
            Status
            <select className="mt-1 w-full rounded-xl border border-plum-100 px-3 py-2" value={form.status} onChange={(event) => update("status", event.target.value)}>
              <option value="active">Active</option>
              <option value="inactive">Inactive</option>
            </select>
          </label>
          <div className="md:col-span-2 flex gap-2">
            <button className="rounded-xl bg-plum-700 px-4 py-2 text-sm font-semibold text-white" type="submit">{editing ? "Save centre" : "Create centre"}</button>
            <button className="rounded-xl border border-plum-100 px-4 py-2 text-sm" type="button" onClick={() => setOpen(false)}>Cancel</button>
          </div>
        </form>
      )}
      <div className="grid gap-4 md:grid-cols-2">
        {centres.map((centre) => (
          <article key={centre.id} className="rounded-2xl bg-white p-5 shadow-card">
            <div className="flex items-start justify-between gap-3">
              <div>
                <h2 className="text-lg font-semibold">{centre.name}</h2>
                <p className="text-sm text-slate-500">{centre.code} · {centre.state}</p>
              </div>
              <span className="rounded-full bg-plum-50 px-2 py-1 text-xs capitalize text-plum-800">{centre.status}</span>
            </div>
            <p className="mt-3 text-sm text-slate-600">{centre.pia}</p>
            <p className="text-sm text-slate-500">{centre.sanction_order}</p>
            <p className="mt-3 text-sm">{centre.batch_count} batches · {centre.candidate_count} candidates</p>
            <div className="mt-4 flex gap-2">
              <Link className="rounded-xl bg-plum-700 px-3 py-2 text-sm font-semibold text-white" to={`/centres/${centre.id}`}>View batches</Link>
              {role !== "centre" && (
                <button type="button" className="rounded-xl border border-plum-100 px-3 py-2 text-sm font-semibold" onClick={() => { setEditing(centre.id); setForm({ ...EMPTY, ...centre }); setOpen(true); }}>Edit centre</button>
              )}
            </div>
          </article>
        ))}
        {centres.length === 0 && <p className="text-sm text-slate-500">No training centre yet.</p>}
      </div>
    </div>
  );
}

function Field({ label, value, onChange }) {
  return (
    <label className="text-sm text-slate-600">
      {label}
      <input className="mt-1 w-full rounded-xl border border-plum-100 px-3 py-2 text-ink" value={value ?? ""} onChange={(event) => onChange(event.target.value)} />
    </label>
  );
}
