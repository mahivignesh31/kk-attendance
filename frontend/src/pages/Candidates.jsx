import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { percent } from "../utils";

export default function Candidates() {
  const [rows, setRows] = useState(null);
  const [error, setError] = useState("");
  const [query, setQuery] = useState("");
  const [confirming, setConfirming] = useState(false);
  const [message, setMessage] = useState("");

  function load() {
    api("/api/candidates").then((payload) => setRows(payload.candidates)).catch((err) => setError(err.message));
  }

  useEffect(() => {
    load();
  }, []);

  async function deleteAll() {
    setError("");
    try {
      const result = await api("/api/candidates/delete-all", { method: "POST" });
      setConfirming(false);
      setMessage(`${result.deleted} candidates were removed from every batch. The batches are still here.`);
      load();
    } catch (err) {
      setError(err.message);
    }
  }

  const visible = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!rows) return [];
    return rows.filter((row) => !needle || `${row.name} ${row.candidate_id}`.toLowerCase().includes(needle));
  }, [rows, query]);

  if (error) return <p className="text-sm text-red-700">{error}</p>;
  if (!rows) return <p className="text-sm text-slate-500">Loading candidates…</p>;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold">Candidates</h1>
          <p className="mt-1 text-sm text-slate-500">{rows.length} records from the master workbook.</p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <input className="rounded-xl border border-plum-100 bg-white px-3 py-2 text-sm" placeholder="Search" value={query} onChange={(event) => setQuery(event.target.value)} />
          <button type="button" className="rounded-xl border border-red-200 px-3 py-2 text-sm font-semibold text-red-800" onClick={() => setConfirming(true)}>Delete all candidates</button>
        </div>
      </div>
      {message && <p className="rounded-xl bg-emerald-50 px-4 py-3 text-sm text-emerald-800">{message}</p>}
      {confirming && (
        <div className="rounded-2xl border border-red-200 bg-white p-4">
          <p className="text-sm text-slate-700">This removes every candidate in every batch, and their local attendance. The batches and the Kaushal portal stay.</p>
          <div className="mt-3 flex gap-2">
            <button type="button" className="rounded-xl bg-red-700 px-3 py-2 text-sm font-semibold text-white" onClick={deleteAll}>Delete all candidates</button>
            <button type="button" className="rounded-xl border border-plum-100 px-3 py-2 text-sm" onClick={() => setConfirming(false)}>Cancel</button>
          </div>
        </div>
      )}
      <div className="overflow-auto rounded-2xl bg-white shadow-card">
        <table className="w-full min-w-[760px] text-left text-sm">
          <thead className="text-slate-500">
            <tr>
              {["Name", "KP Id", "Status", "Working days", "Full", "Half", "Absent", "Credits", "Classroom %"].map((heading) => (
                <th key={heading} className="px-4 py-3 font-medium">{heading}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {visible.map((row) => (
              <tr key={row.id} className="border-t border-plum-50">
                <td className="px-4 py-3"><Link className="font-medium text-plum-800" to={`/candidates/${row.id}`}>{row.name}</Link></td>
                <td className="px-4 py-3">{row.candidate_id}</td>
                <td className="px-4 py-3">{row.candidate_status || "—"}</td>
                <td className="px-4 py-3">{row.eligible_days}</td>
                <td className="px-4 py-3">{row.full_days}</td>
                <td className="px-4 py-3">{row.half_days}</td>
                <td className="px-4 py-3">{row.absent_days}</td>
                <td className="px-4 py-3">{row.attendance_credits}</td>
                <td className="px-4 py-3 font-semibold">{percent(row.verified_percent)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
