import { AlertTriangle, Check, Minus, X } from "lucide-react";

const VIEW = {
  full_day: { label: "Present", tone: "bg-emerald-50 text-emerald-700 ring-emerald-200", mark: "check" },
  half_day: { label: "Half day", tone: "bg-amber-50 text-amber-800 ring-amber-200", mark: "half" },
  absent: { label: "Absent", tone: "bg-red-50 text-red-700 ring-red-200", mark: "cross" },
  insufficient: { label: "Insufficient hours", tone: "bg-red-50 text-red-700 ring-red-200", mark: "cross" },
  incomplete: { label: "Incomplete record", tone: "bg-orange-50 text-orange-700 ring-orange-200", mark: "warn" },
  not_verified: { label: "Not yet verified", tone: "bg-slate-100 text-slate-400 ring-slate-200", mark: "dash" },
  before_enrollment: { label: "Before enrollment", tone: "bg-slate-50 text-slate-300 ring-slate-100", mark: "dash" },
  sunday: { label: "Sunday", tone: "bg-slate-50 text-slate-300 ring-slate-100", mark: "sun" },
  future: { label: "Future date", tone: "bg-slate-50 text-slate-300 ring-slate-100", mark: "dash" },
  not_eligible: { label: "Not eligible", tone: "bg-slate-50 text-slate-300 ring-slate-100", mark: "dash" },
};

export function statusMeta(status) {
  return VIEW[status] || { label: status || "Unknown", tone: "bg-slate-100 text-slate-500 ring-slate-200", mark: "dash" };
}

export default function StatusMark({ status, warning = false, grace = false, onClick }) {
  const meta = statusMeta(status);
  const className = `inline-flex h-8 w-8 items-center justify-center rounded-lg text-sm font-semibold ring-1 ${meta.tone}`;
  const title = grace && status === "full_day" ? "Present · grace occasion" : meta.label;
  const content = (
    <>
      {meta.mark === "check" && <Check size={16} strokeWidth={2.5} />}
      {meta.mark === "cross" && <X size={16} strokeWidth={2.5} />}
      {meta.mark === "dash" && <Minus size={16} />}
      {meta.mark === "warn" && <AlertTriangle size={15} />}
      {meta.mark === "half" && <span aria-hidden="true">½</span>}
      {meta.mark === "sun" && <span aria-hidden="true">S</span>}
      {grace && status === "full_day" && (
        <span className="absolute -right-1 -top-1 h-2.5 w-2.5 rounded-full bg-gold ring-2 ring-white" title="Grace occasion" />
      )}
      {warning && !grace && <span className="absolute -right-1 -top-1 h-2 w-2 rounded-full bg-orange-500" />}
      {warning && grace && <span className="absolute -left-1 -top-1 h-2 w-2 rounded-full bg-orange-500" />}
    </>
  );
  if (!onClick) {
    return (
      <span className={`relative ${className}`} title={title} aria-label={title}>
        {content}
      </span>
    );
  }
  return (
    <button type="button" className={`relative ${className} transition hover:scale-105`} title={title} aria-label={title} onClick={onClick}>
      {content}
    </button>
  );
}
