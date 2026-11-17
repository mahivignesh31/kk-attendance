import { useEffect, useState } from "react";
import { api } from "../api";
import { stamp } from "../utils";

export default function Reconciliation() {
  const [issues, setIssues] = useState(null);
  const [error, setError] = useState("");

  const load = () => api("/api/reconciliation").then((payload) => setIssues(payload.issues)).catch((err) => setError(err.message));
  useEffect(() => { load(); }, []);

  async function resolve(id) {
    await api(`/api/reconciliation/${id}/resolve`, { method: "POST" });
    load();
  }

  if (error) return <p className="text-sm text-red-700">{error}</p>;
  if (!issues) return <p className="text-sm text-slate-500">Loading issues…</p>;

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-2xl font-semibold">Reconciliation</h1>
        <p className="mt-1 max-w-3xl text-sm text-slate-500">These are differences between the portal summary, the candidate rows, and the master list. Resolving an issue hides it here. It does not change the Kaushal portal.</p>
      </div>
      {issues.length === 0 ? <p className="rounded-2xl bg-white p-5 text-sm text-slate-500 shadow-card">No open issues.</p> : (
        <div className="space-y-3">
          {issues.map((issue) => (
            <article key={issue.id} className="rounded-2xl bg-white p-4 shadow-card">
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div>
                  <div className="text-xs uppercase tracking-wide text-slate-400">{issue.severity} · {issue.issue_type} · {issue.attendance_date || "—"}</div>
                  <p className="mt-1 text-sm">{issue.message}</p>
                  {issue.candidate_name && <p className="mt-1 text-xs text-slate-500">{issue.candidate_name} · {issue.candidate_id}</p>}
                  <p className="mt-1 text-xs text-slate-400">{stamp(issue.created_at)}</p>
                </div>
                <button type="button" className="rounded-xl border border-plum-100 px-3 py-2 text-sm font-medium" onClick={() => resolve(issue.id)}>Mark reviewed</button>
              </div>
            </article>
          ))}
        </div>
      )}
    </div>
  );
}
