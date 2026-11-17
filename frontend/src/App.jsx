import { useEffect, useState } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import Layout from "./components/Layout";
import Dashboard from "./pages/Dashboard";
import Centres from "./pages/Centres";
import CentreDetail from "./pages/CentreDetail";
import Matrix from "./pages/Matrix";
import Candidates from "./pages/Candidates";
import CandidateProfile from "./pages/CandidateProfile";
import SyncPage from "./pages/SyncPage";
import Settings from "./pages/Settings";
import Login from "./pages/Login";
import { api } from "./api";

export default function App() {
  const [user, setUser] = useState(undefined);

  useEffect(() => {
    api("/api/auth/me").then(setUser).catch(() => setUser(null));
  }, []);

  if (user === undefined) {
    return <p className="p-8 text-sm text-slate-500">Checking administrator session…</p>;
  }
  if (!user) return <Login onSuccess={setUser} />;

  return (
    <Routes>
      <Route element={<Layout onSignedOut={() => setUser(null)} />}>
        <Route index element={<Dashboard />} />
        <Route path="centres" element={<Centres />} />
        <Route path="centres/:id" element={<CentreDetail />} />
        <Route path="matrix" element={<Matrix />} />
        <Route path="candidates" element={<Candidates />} />
        <Route path="candidates/:id" element={<CandidateProfile />} />
        <Route path="sync" element={<SyncPage />} />
        <Route path="settings" element={<Settings />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  );
}
