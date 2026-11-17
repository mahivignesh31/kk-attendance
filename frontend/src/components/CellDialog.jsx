import { useEffect, useState } from "react";
import { api } from "../api";
import { hours, longDate, stamp } from "../utils";
import Modal from "./Modal";
import { statusMeta } from "./StatusMark";

export default function CellDialog({ candidatePk, date, onClose }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState("");

  useEffect(() => {
    let active = true;
    api(`/api/attendance/cell?candidate_pk=${candidatePk}&date=${date}`)
      .then((payload) => {
        if (active) setData(payload);
      })
      .catch((err) => {
        if (active) setError(err.message);
      });
    return () => {
      active = false;
    };
  }, [candidatePk, date]);

  const record = data?.record;
  const meta = statusMeta(data?.cell?.status);

  return (
    <Modal title="Attendance record" onClose={onClose}>
      {error && <p className="text-sm text-red-700">{error}</p>}
      {!data && !error && <p className="text-sm text-slate-500">Loading the saved record…</p>}
      {data && (
        <div className="space-y-4">
          <div className="flex items-start justify-between gap-3">
            <div>
              <p className="text-xl font-semibold">{data.candidate.name}</p>
              <p className="text-sm text-slate-500">Candidate Id {data.candidate.candidate_id}</p>
            </div>
            <span className={`rounded-full px-3 py-1 text-sm font-medium ring-1 ${meta.tone}`}>{meta.label}</span>
          </div>
          <dl className="grid grid-cols-2 gap-3 text-sm">
            <Field label="Attendance date" value={longDate(data.date)} />
            <Field label="Attendance Id" value={data.candidate.attendance_id || "—"} />
            <Field label="In time" value={record?.in_time || "—"} />
            <Field label="Out time" value={record?.out_time || "—"} />
            <Field label="Elapsed hours" value={record?.elapsed_label || hours(record?.elapsed_minutes)} />
            <Field label="Lunch deduction" value={record?.lunch_label || "—"} />
            <Field label="Qualifying hours" value={record?.qualifying_label || "—"} />
            <Field label="Official portal status" value={record?.official_label || "No portal row"} />
            <Field label="Calculated dashboard status" value={record?.calculated_label || meta.label} />
            <Field label="Data source" value={record?.data_source || "No portal record"} />
            <Field label="Last synced" value={stamp(record?.last_synced_at)} />
            <Field label="Device" value={record?.device_type || "—"} />
          </dl>
          {data.sessions?.length > 0 && (
            <div>
              <h3 className="mb-2 text-sm font-semibold">Sessions</h3>
              <div className="overflow-hidden rounded-xl border border-plum-100">
                <table className="w-full text-left text-sm">
                  <thead className="bg-plum-50 text-slate-500">
                    <tr>
                      <th className="px-3 py-2 font-medium">In</th>
                      <th className="px-3 py-2 font-medium">Out</th>
                      <th className="px-3 py-2 font-medium">Device</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.sessions.map((session) => (
                      <tr key={session.id} className="border-t border-plum-50">
                        <td className="px-3 py-2">{session.in_time || "—"}</td>
                        <td className="px-3 py-2">{session.out_time || "—"}</td>
                        <td className="px-3 py-2">{session.device_type || "—"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
          <p className="text-xs leading-5 text-slate-500">
            DDU-GKY 2.0 one-shift rule for a day longer than 4 hours. Fifteen minutes late or early is condoned. Up to 1 hour is a full day, and only 2 such days count in any 15 calendar days. More than 1 hour is a half day. More than 4 hours late or early is absent. An out time of 00:00:00 means there was no check-out, so that one punch is a full day. A check-out at the same minute as the check-in is a real exit and is judged by the table. Lunch is not subtracted.
          </p>
        </div>
      )}
    </Modal>
  );
}

function Field({ label, value }) {
  return (
    <div className="rounded-xl bg-plum-50 px-3 py-2">
      <dt className="text-xs uppercase tracking-wide text-slate-500">{label}</dt>
      <dd className="mt-1 font-medium text-ink">{value}</dd>
    </div>
  );
}
