import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../api";
import { hours, longDate, percent } from "../utils";
import StatusMark from "../components/StatusMark";
import CellDialog from "../components/CellDialog";

export default function CandidateProfile() {
  const { id } = useParams();
  const [data, setData] = useState(null);
  const [error, setError] = useState("");
  const [cell, setCell] = useState(null);

  useEffect(() => {
    api(`/api/candidates/${id}`).then(setData).catch((err) => setError(err.message));
  }, [id]);

  if (error) return <p className="text-sm text-red-700">{error}</p>;
  if (!data) return <p className="text-sm text-slate-500">Loading profile…</p>;
  const person = data.candidate;

  return (
    <div className="space-y-6">
      <Link to="/candidates" className="text-sm font-medium text-plum-700">All candidates</Link>
      <div className="rounded-2xl bg-white p-5 shadow-card">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h1 className="text-2xl font-semibold">{person.name}</h1>
            <p className="mt-1 text-sm text-slate-500">{person.candidate_status || "Status unknown"} · Batch {person.batch_registration_number || "—"}</p>
          </div>
          <div className="text-right">
            <div className="text-3xl font-semibold text-plum-800">{percent(person.verified_percent)}</div>
            <div className="text-xs text-slate-500">Classroom attendance %</div>
          </div>
        </div>
        <dl className="mt-5 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <Info label="KP Id" value={person.candidate_id} />
          <Info label="Enrollment date" value={longDate(person.enrollment_date)} />
          <Info label="OJT start date" value={longDate(person.ojt_start_date)} />
          <Info label="Full days" value={person.full_days} />
          <Info label="Half days" value={person.half_days} />
          <Info label="Absent days" value={person.absent_days} />
          <Info label="Insufficient hours" value={person.insufficient_days} />
          <Info label="Incomplete records" value={person.incomplete_days} />
          <Info label="Not yet verified" value={person.unverified_days} />
          <Info label="Qualifying hours" value={person.qualifying_label || hours(person.qualifying_minutes)} />
          <Info label="Total working days" value={person.eligible_days} />
          <Info label="Attendance credits" value={person.attendance_credits} />
        </dl>
        <p className="mt-4 text-xs leading-5 text-slate-500">
          Classroom attendance % = ((full days + half days × 0.5) / total working days) × 100, rounded to 2 decimals. Only the classroom marks are used: Present, Half day, and Absent. RF and OJT are not included. A portal Present mark still counts when RF is also listed. Otherwise the DDU-GKY 2.0 one-shift table is used. 19 full days and 1 half day out of 28 working days is 69.64%. Sundays and configured holidays are left out. A day before enrollment, and a completed working day with no classroom mark, are absent. Today is left out until that day's attendance is finalized.
        </p>
      </div>

      <section className="rounded-2xl bg-white p-5 shadow-card">
        <h2 className="font-semibold">Monthly summary</h2>
        {data.monthly.length === 0 ? <p className="mt-3 text-sm text-slate-500">No verified month yet.</p> : (
          <div className="mt-3 grid gap-3 sm:grid-cols-3">
            {data.monthly.map((month) => (
              <article key={month.month} className="rounded-xl bg-plum-50 p-3">
                <div className="text-sm text-slate-500">{month.label}</div>
                <div className="mt-1 text-xl font-semibold">{percent(month.verified_percent)}</div>
                <div className="text-xs text-slate-500">{month.verified_days} verified days · {month.qualifying_hours || 0} hours</div>
              </article>
            ))}
          </div>
        )}
      </section>

      <section className="rounded-2xl bg-white p-5 shadow-card">
        <h2 className="mb-3 font-semibold">Date-wise timeline</h2>
        {data.timeline.length === 0 ? <p className="text-sm text-slate-500">The portal date list is empty.</p> : (
          <div className="overflow-auto">
            <table className="w-full min-w-[680px] text-left text-sm">
              <thead className="text-slate-500">
                <tr>
                  {["Date", "Status", "In", "Out", "Qualifying", "Official"].map((heading) => <th key={heading} className="py-2 font-medium">{heading}</th>)}
                </tr>
              </thead>
              <tbody>
                {data.timeline.map((row) => (
                  <tr key={row.date} className="border-t border-plum-50">
                    <td className="py-2">{longDate(row.date)}</td>
                    <td className="py-2"><StatusMark status={row.status} warning={row.warning} onClick={() => setCell(row.date)} /></td>
                    <td className="py-2">{row.in_time || "—"}</td>
                    <td className="py-2">{row.out_time || "—"}</td>
                    <td className="py-2">{row.qualifying_label || "—"}</td>
                    <td className="py-2">{row.official_label || "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
      {cell && <CellDialog candidatePk={person.id} date={cell} onClose={() => setCell(null)} />}
    </div>
  );
}

function Info({ label, value }) {
  return (
    <div className="rounded-xl bg-plum-50 px-3 py-2">
      <div className="text-xs uppercase tracking-wide text-slate-500">{label}</div>
      <div className="mt-1 font-medium">{value}</div>
    </div>
  );
}
