import { useEffect } from "react";

export default function Modal({ title, onClose, children }) {
  useEffect(() => {
    const onKey = (event) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div className="fixed inset-0 z-40 flex items-end justify-center bg-ink/40 p-4 sm:items-center" onMouseDown={onClose}>
      <div
        className="max-h-[88vh] w-full max-w-2xl overflow-auto rounded-2xl bg-white shadow-card"
        role="dialog"
        aria-modal="true"
        aria-label={title}
        onMouseDown={(event) => event.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b border-plum-100 px-5 py-4">
          <h2 className="text-lg font-semibold">{title}</h2>
          <button type="button" className="rounded-lg px-2 py-1 text-sm text-slate-500 hover:bg-plum-50" onClick={onClose}>
            Close
          </button>
        </div>
        <div className="px-5 py-4">{children}</div>
      </div>
    </div>
  );
}
