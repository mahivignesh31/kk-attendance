import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { percent } from "../utils";
import StatusMark from "../components/StatusMark";
import CellDialog from "../components/CellDialog";

const LEGEND = ["full_day", "half_day", "absent", "incomplete", "not_verified", "future"];

export default function Matrix() {
  const [data, setData] = useState(null);
  const [error, setError] = useState("");
  const [query, setQuery] = useState("");
  const [cell, setCell] = useState(null);

  useEffect(() => {
    api("/api/matrix").then(setData).catch((err) => setError(err.message));
  }, []);

  const rows = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!data) return [];
    return data.rows.filter((row) => !needle || row.name.toLowerCase().includes(needle) || row.candidate_id.includes(needle));
  }, [data, query]);

  if (error) return <p className="text-sm text-red-700">{error}</p>;
  if (!data) return <p className="text-sm text-slate-500">Loading the matrix…</p>;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold">Candidate attendance matrix</h1>
          <p className="mt-1 text-sm text-slate-500">{data.candidate_count} candidates · {data.dates.length} dates (includes upcoming working days when applicable). Click a cell for the check-in record.</p>
        </div>
        <input className="rounded-xl border border-plum-100 bg-white px-3 py-2 text-sm" placeholder="Search name or id" value={query} onChange={(event) => setQuery(event.target.value)} />
      </div>
      <div className="flex flex-wrap gap-3 text-xs text-slate-600">
        {LEGEND.map((status) => (
          <span key={status} className="inline-flex items-center gap-2">
            <StatusMark status={status} />
            <span>{status === "full_day" ? "Present" : status === "half_day" ? "Half day" : status === "absent" ? "Absent" : status === "incomplete" ? "Incomplete" : status === "future" ? "Upcoming" : "N/A"}</span>
          </span>
        ))}
        <span className="inline-flex items-center gap-2">
          <StatusMark status="full_day" grace />
          <span>Present · grace</span>
        </span>
      </div>
      {data.dates.length === 0 ? (
        <div className="rounded-2xl bg-white p-6 shadow-card">
          <p className="text-sm text-slate-600">No attendance dates have been read from the portal. The 35 master candidates are still listed below, with no attendance invented for them.</p>
          <ul className="mt-4 grid gap-2 sm:grid-cols-2">
            {rows.map((row) => (
              <li key={row.id}><Link className="text-sm font-medium text-plum-800" to={`/candidates/${row.id}`}>{row.name}</Link></li>
            ))}
          </ul>
        </div>
      ) : (
        <div className="matrix-scroll rounded-2xl border border-plum-100 bg-white shadow-card">
          <table className="min-w-full border-separate border-spacing-0 text-sm">
            <thead>
              <tr>
                <th className="sticky-col border-b border-plum-100 px-4 py-3 text-left font-medium">Candidate</th>
                {data.dates.map((column) => (
                  <th key={column.date} className={`border-b border-plum-100 px-2 py-3 text-center font-medium ${column.sunday || column.upcoming ? "text-slate-400" : ""}`}>
                    <div>{column.label}</div>
                    <div className="text-[11px] font-normal text-slate-400">{column.upcoming ? "Upcoming" : column.weekday}</div>
                  </th>
                ))}
                <th className="border-b border-plum-100 px-3 py-3 text-right font-medium">Classroom %</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.id}>
                  <td className="sticky-col border-b border-plum-50 px-4 py-2">
                    <Link className="font-medium text-plum-800" to={`/candidates/${row.id}`}>{row.name}</Link>
                    <div className="text-xs text-slate-400">{row.candidate_id}</div>
                  </td>
                  {data.dates.map((column) => {
                    const item = row.cells[column.date];
                    return (
                      <td key={column.date} className="border-b border-plum-50 px-2 py-2 text-center">
                        <StatusMark status={item.status} warning={item.warning} grace={item.grace} onClick={() => setCell({ id: row.id, date: column.date })} />
                      </td>
                    );
                  })}
                  <td className="border-b border-plum-50 px-3 py-2 text-right">
                    <div className="font-semibold">{percent(row.verified_percent)}</div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {cell && <CellDialog candidatePk={cell.id} date={cell.date} onClose={() => setCell(null)} />}
    </div>
  );
}
