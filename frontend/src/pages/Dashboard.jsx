import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Bar, BarChart, CartesianGrid, Cell, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { api, apiUrl } from "../api";
import { longDate, percent, stamp } from "../utils";

export default function Dashboard() {
  const [data, setData] = useState(null);
  const [error, setError] = useState("");
  const [date, setDate] = useState("");

  useEffect(() => {
    const query = date ? `?date=${date}` : "";
    api(`/api/dashboard${query}`)
      .then((payload) => {
        setData(payload);
        setError("");
        if (!date && payload.selected_date) setDate(payload.selected_date);
      })
      .catch((err) => setError(err.message));
  }, [date]);

  if (error) return <p className="text-sm text-red-700">{error}</p>;
  if (!data) return <p className="text-sm text-slate-500">Loading attendance…</p>;

  const cards = data.cards;
  const synced = data.selected_detail_synced;
  const summary = cards.portal_summary;
  const percentSeries = data.daily.filter((item) => item.verified_percent !== null);
  const portalSeries = data.daily.filter((item) => item.portal_present !== null || item.portal_absent !== null);
  const month = data.selected_date ? data.selected_date.slice(0, 7) : "";

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold">Batch attendance</h1>
          <p className="mt-1 text-sm text-slate-500">
            {data.batch.pia} · {data.batch.state}
          </p>
        </div>
        <label className="text-sm text-slate-600">
          Report date
          <select className="mt-1 block rounded-xl border border-plum-100 bg-white px-3 py-2" value={date} onChange={(event) => setDate(event.target.value)}>
            {data.portal_dates.length === 0 && <option value="">No portal dates yet</option>}
            {data.portal_dates.map((item) => (
              <option key={item} value={item}>{longDate(item)}</option>
            ))}
          </select>
        </label>
      </div>

      <section className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
        <Card label="Training centres" value={cards.training_centres ?? 0} hint={data?.user?.role === "centre" ? "Your centre only" : "Created by the administrator"} />
        <Card label="Active batches" value={cards.active_batches ?? 0} hint={data?.user?.role === "centre" ? "Batches at your centre" : "Across every centre"} />
        <Card label="Total candidates" value={cards.total_candidates} hint="Batch on screen" />
        <Card label="Today's classroom attendance" value={percent(cards.average_verified_percent)} hint="Classroom only. RF and OJT are not included." />
        <Card label="Below 75%" value={cards.below_threshold ?? 0} hint="Candidates on the batch on screen" />
        <Card label="Last sync" value={cards.last_sync ? stamp(cards.last_sync.finished_at) : "Never"} hint={cards.last_sync?.message || "Portal not read yet"} />
      </section>
      <section className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <Card label="Total candidates" value={cards.total_candidates} hint="Master workbook" />
        <Card label="Full day" value={synced ? cards.full_day : null} hint={dayHint(synced, summary, "present", data.selected_date)} />
        <Card label="Half day" value={synced ? cards.half_day : null} hint={dayHint(synced, summary, "half_day", data.selected_date)} />
        <Card label="Absent" value={synced ? cards.absent : null} hint={dayHint(synced, summary, "absent", data.selected_date)} />
        <Card label="Classroom attendance" value={percent(cards.average_verified_percent)} hint="Classroom only. RF and OJT are not included." />
        <Card label="Total days" value={cards.verified_training_days} hint="Batch start through today, Sundays left out" />
        <Card label="Last successful sync" value={cards.last_sync ? stamp(cards.last_sync.finished_at) : "Never"} hint={cards.last_sync?.message || "Portal not read yet"} />
        <Card label="Not yet verified" value={synced ? cards.not_verified : null} hint={synced ? "Eligible candidates without a reconciled row" : "Waiting for candidate-level sync"} />
      </section>

      <section className="grid gap-4 xl:grid-cols-2">
        <ChartCard title="Daily attendance percentage" note="Calculated from reconciled candidate hours. Days that are not verified are left off this chart.">
          {percentSeries.length === 0 ? <Empty /> : (
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={percentSeries}>
                <CartesianGrid stroke="#F1E9F7" vertical={false} />
                <XAxis dataKey="label" tick={{ fontSize: 12 }} />
                <YAxis domain={[0, 100]} tick={{ fontSize: 12 }} unit="%" />
                <Tooltip />
                <Line type="monotone" dataKey="verified_percent" name="Attendance %" stroke="#5B2A86" strokeWidth={2.5} dot={false} />
              </LineChart>
            </ResponsiveContainer>
          )}
        </ChartCard>
        <ChartCard title="Present, half day and absent" note="Official portal summary counts. These are headcounts, not the local hour calculation.">
          {portalSeries.length === 0 ? <Empty /> : (
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={portalSeries}>
                <CartesianGrid stroke="#F1E9F7" vertical={false} />
                <XAxis dataKey="label" tick={{ fontSize: 12 }} />
                <YAxis allowDecimals={false} tick={{ fontSize: 12 }} />
                <Tooltip />
                <Bar dataKey="portal_present" name="Present" fill="#5B2A86" radius={4} />
                <Bar dataKey="portal_half_day" name="Half day" fill="#D97706" radius={4} />
                <Bar dataKey="portal_absent" name="Absent" fill="#DC2626" radius={4} />
              </BarChart>
            </ResponsiveContainer>
          )}
        </ChartCard>
        <ChartCard title="Candidate attendance distribution" note="Candidates with at least one verified eligible day.">
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={data.distribution}>
              <CartesianGrid stroke="#F1E9F7" vertical={false} />
              <XAxis dataKey="bucket" tick={{ fontSize: 11 }} interval={0} />
              <YAxis allowDecimals={false} tick={{ fontSize: 12 }} />
              <Tooltip />
              <Bar dataKey="count" name="Candidates" radius={6}>
                {data.distribution.map((entry) => (
                  <Cell key={entry.bucket} fill={entry.bucket.startsWith("Below") || entry.bucket.startsWith("50") ? "#D97706" : "#5B2A86"} />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </ChartCard>
        <ChartCard title="Monthly attendance trend" note="Average of the daily verified percentages in each month.">
          {data.monthly.length === 0 ? <Empty /> : (
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={data.monthly}>
                <CartesianGrid stroke="#F1E9F7" vertical={false} />
                <XAxis dataKey="label" tick={{ fontSize: 12 }} />
                <YAxis domain={[0, 100]} tick={{ fontSize: 12 }} unit="%" />
                <Tooltip />
                <Bar dataKey="verified_percent" name="Attendance %" fill="#5B2A86" radius={6} />
              </BarChart>
            </ResponsiveContainer>
          )}
        </ChartCard>
      </section>

      <section className="rounded-2xl bg-white p-5 shadow-card">
        <div className="mb-4 flex items-center justify-between gap-3">
          <div>
            <h2 className="font-semibold">Candidates below {data.rules.low_attendance_threshold}%</h2>
            <p className="text-sm text-slate-500">A day with no attendance mark counts as absent and lowers this percentage.</p>
          </div>
          <a className="text-sm font-semibold text-plum-700" href={apiUrl("/api/export/low-attendance")}>Download report</a>
        </div>
        {data.below_threshold.length === 0 ? (
          <p className="text-sm text-slate-500">No verified attendance is below the threshold yet.</p>
        ) : (
          <div className="overflow-auto">
            <table className="w-full text-left text-sm">
              <thead className="text-slate-500">
                <tr>
                  <th className="py-2 font-medium">Candidate</th>
                  <th className="py-2 font-medium">Attendance %</th>
                  <th className="py-2 font-medium">Full</th>
                  <th className="py-2 font-medium">Half</th>
                  <th className="py-2 font-medium">Absent</th>
                </tr>
              </thead>
              <tbody>
                {data.below_threshold.map((row) => (
                  <tr key={row.id} className="border-t border-plum-50">
                    <td className="py-2"><Link className="font-medium text-plum-800" to={`/candidates/${row.id}`}>{row.name}</Link></td>
                    <td className="py-2">{percent(row.verified_percent)}</td>
                    <td className="py-2">{row.full_days}</td>
                    <td className="py-2">{row.half_days}</td>
                    <td className="py-2">{row.absent_days}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section className="flex flex-wrap gap-2">
        <Export href={data.selected_date ? `/api/export/daily?date=${data.selected_date}` : ""} label="Download Daily Attendance" />
        <Export href={month ? `/api/export/monthly?month=${month}` : ""} label="Download Monthly Attendance" />
        <Export href="/api/export/candidates" label="Download Candidate-Wise Attendance" />
        <Export href="/api/export/matrix" label="Download Full Attendance Matrix" />
        <Export href="/api/export/low-attendance" label="Download Low Attendance Report" />
      </section>
    </div>
  );
}

function dayHint(synced, summary, key, selected) {
  if (!selected) return "No portal date has been read";
  if (!synced) {
    const value = summary?.[key];
    if (value === null || value === undefined) return "No candidate-level hours for this date";
    const label = key === "half_day" ? "half day" : key;
    return `Portal summary ${label}: ${value}. Hours are not verified yet.`;
  }
  return longDate(selected);
}

function Card({ label, value, hint }) {
  const shown = value === null || value === undefined || value === "" ? "—" : value;
  return (
    <article className="rounded-2xl bg-white p-4 shadow-card">
      <p className="text-xs font-medium uppercase tracking-wide text-slate-500">{label}</p>
      <p className="mt-2 text-2xl font-semibold">{shown}</p>
      <p className="mt-1 text-xs leading-5 text-slate-500">{hint}</p>
    </article>
  );
}

function ChartCard({ title, note, children }) {
  return (
    <article className="rounded-2xl bg-white p-4 shadow-card">
      <h2 className="font-semibold">{title}</h2>
      <p className="mb-3 text-xs leading-5 text-slate-500">{note}</p>
      <div className="h-64">{children}</div>
    </article>
  );
}

function Empty() {
  return <div className="grid h-full place-items-center rounded-xl border border-dashed border-plum-100 px-6 text-center text-sm text-slate-500">Sync the Kaushal portal to plot attendance. This chart does not estimate missing days.</div>;
}

function Export({ href, label }) {
  if (!href) return null;
  return <a className="rounded-xl border border-plum-100 bg-white px-3 py-2 text-sm font-medium text-plum-800 hover:bg-plum-50" href={apiUrl(href)}>{label}</a>;
}
