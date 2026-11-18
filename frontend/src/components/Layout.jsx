import { useEffect, useState } from "react";
import { NavLink, Outlet, Link } from "react-router-dom";
import { Building2, Grid3X3, LayoutDashboard, LogOut, Menu, RefreshCw, Settings, Users, X } from "lucide-react";
import { api } from "../api";

const LINKS = [
  ["/", "Dashboard", LayoutDashboard],
  ["/centres", "Training Centres", Building2],
  ["/matrix", "Attendance", Grid3X3],
  ["/candidates", "Candidates", Users],
  ["/sync", "Sync Attendance", RefreshCw],
  ["/settings", "Settings", Settings],
];

export default function Layout({ onSignedOut }) {
  const [boot, setBoot] = useState(null);
  const [offline, setOffline] = useState(false);
  const [open, setOpen] = useState(false);

  useEffect(() => {
    let active = true;
    const load = () => {
      api("/api/bootstrap")
        .then((data) => {
          if (!active) return;
          setBoot(data);
          setOffline(false);
        })
        .catch((err) => {
          if (!active) return;
          if (err.status === 401) onSignedOut();
          else setOffline(true);
        });
    };
    load();
    const timer = setInterval(load, 5000);
    return () => {
      active = false;
      clearInterval(timer);
    };
  }, []);

  const running = ["running", "awaiting_login", "cancelling"].includes(boot?.sync?.status);
  const centreLogin = boot?.user?.role === "centre";
  // Centres view reports only — no Settings, Centres admin list, or Sync.
  const links = centreLogin
    ? LINKS.filter(([to]) => to !== "/settings" && to !== "/centres" && to !== "/sync")
    : LINKS;

  return (
    <div className="min-h-screen bg-plum-50 text-ink">
      <aside className={`fixed inset-y-0 left-0 z-30 w-72 border-r border-plum-100 bg-white transition md:translate-x-0 ${open ? "translate-x-0" : "-translate-x-full"}`}>
        <div className="flex items-center gap-3 px-5 py-5">
          <div className="grid h-11 w-11 place-items-center rounded-2xl bg-plum-700 font-semibold text-white">KK</div>
          <div>
            <div className="font-semibold leading-tight">Attendance monitor</div>
            <div className="text-xs text-slate-500">KKERS · Ongole</div>
          </div>
        </div>
        <div className="mx-5 h-px bg-gradient-to-r from-gold to-transparent" />
        <nav className="mt-4 space-y-1 px-3">
          {links.map(([to, label, Icon]) => (
            <NavLink
              key={label}
              to={to}
              end={to === "/"}
              onClick={() => setOpen(false)}
              className={({ isActive }) =>
                `flex items-center gap-3 rounded-xl px-3 py-2.5 text-sm font-medium ${isActive ? "bg-plum-700 text-white" : "text-slate-600 hover:bg-plum-50"}`
              }
            >
              <Icon size={18} />
              <span>{label}</span>
            </NavLink>
          ))}
        </nav>
        <div className="absolute bottom-0 left-0 right-0 border-t border-plum-100 p-4 text-xs leading-5 text-slate-500">
          Read-only local reports. Official Kaushal records are not edited from this dashboard.
        </div>
      </aside>
      {open && <button className="fixed inset-0 z-20 bg-ink/30 md:hidden" aria-label="Close menu" onClick={() => setOpen(false)} />}
      <div className="md:pl-72">
        <header className="sticky top-0 z-10 flex items-center justify-between gap-3 border-b border-plum-100 bg-white/90 px-4 py-3 backdrop-blur md:px-8">
          <div className="flex items-center gap-3">
            <button type="button" className="rounded-lg p-2 hover:bg-plum-50 md:hidden" onClick={() => setOpen((value) => !value)} aria-label="Open menu">
              {open ? <X size={18} /> : <Menu size={18} />}
            </button>
            <div>
              <div className="text-sm font-semibold">{boot?.user?.centre_name || boot?.active_centre?.name || boot?.batch?.training_center || "Attendance"}</div>
              <div className="mt-1 flex flex-wrap items-end gap-3">
                {!centreLogin && (
                  <label className="block text-xs text-slate-500">
                    Training centre
                    <select
                      className="mt-1 block min-w-[10rem] rounded-lg border border-plum-100 bg-white px-2 py-1 text-sm font-semibold text-ink"
                      value={boot?.active_centre_id || ""}
                      onChange={async (event) => {
                        const id = event.target.value;
                        if (!id) return;
                        await api("/api/centres/activate", { method: "POST", body: JSON.stringify({ centre_id: Number(id) }) });
                        setBoot(await api("/api/bootstrap"));
                      }}
                    >
                      {(boot?.centres || []).length === 0 && <option value="">No centre yet</option>}
                      {(boot?.centres || []).map((centre) => (
                        <option key={centre.id} value={centre.id}>
                          {centre.name}
                        </option>
                      ))}
                    </select>
                  </label>
                )}
                <label className="block text-xs text-slate-500">
                  Batch ID
                  <select
                    className="mt-1 block min-w-[12rem] rounded-lg border border-plum-100 bg-white px-2 py-1 text-sm font-semibold text-ink"
                    value={boot?.batch?.batch_code || ""}
                    onChange={async (event) => {
                      const code = event.target.value;
                      if (!code) return;
                      await api("/api/batches/activate", { method: "POST", body: JSON.stringify({ batch_code: code }) });
                      setBoot(await api("/api/bootstrap"));
                    }}
                  >
                    {(boot?.batches || []).length === 0 && <option value="">No batch yet</option>}
                    {(boot?.batches || []).map((batch) => (
                      <option key={batch.batch_code} value={batch.batch_code}>
                        {batch.batch_code} · {batch.candidate_count} candidates
                      </option>
                    ))}
                  </select>
                </label>
              </div>
            </div>
          </div>
          <div className="flex items-center gap-2">
            {!centreLogin && (
              <Link to="/sync" className="inline-flex items-center gap-2 rounded-xl bg-plum-700 px-3 py-2 text-sm font-semibold text-white hover:bg-plum-800">
                <RefreshCw size={16} className={running ? "animate-spin" : ""} />
                {running ? "Sync in progress" : "Sync portal"}
              </Link>
            )}
            {centreLogin && (
              <span className="rounded-xl bg-plum-50 px-3 py-2 text-xs font-medium text-plum-800">Reports only · sync is admin</span>
            )}
            <button
              type="button"
              className="inline-flex items-center gap-2 rounded-xl border border-plum-100 px-3 py-2 text-sm font-semibold text-slate-700"
              onClick={async () => {
                await api("/api/auth/logout", { method: "POST" });
                onSignedOut();
              }}
            >
              <LogOut size={16} />
              Sign out
            </button>
          </div>
        </header>
        {offline && (
          <div className="mx-4 mt-4 rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-800 md:mx-8">
            The local API is not responding. Start the backend on port 8000, then refresh this page.
          </div>
        )}
        <main className="px-4 py-6 md:px-8">
          <Outlet key={boot?.batch?.batch_code || "none"} />
        </main>
      </div>
    </div>
  );
}
