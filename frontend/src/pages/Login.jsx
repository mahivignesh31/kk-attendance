import { useState } from "react";
import { api } from "../api";

export default function Login({ onSuccess }) {
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(event) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      const user = await api("/api/auth/login", {
        method: "POST",
        body: JSON.stringify({ username, password }),
      });
      onSuccess(user);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="grid min-h-screen place-items-center bg-plum-50 px-4">
      <form onSubmit={submit} className="w-full max-w-md rounded-3xl bg-white p-8 shadow-card">
        <div className="grid h-12 w-12 place-items-center rounded-2xl bg-plum-700 font-semibold text-white">KK</div>
        <h1 className="mt-5 text-2xl font-semibold">Administrator login</h1>
        <p className="mt-2 text-sm text-slate-500">Sign in as the administrator, or with the training centre username and password. A centre login shows only that centre's batches and candidates.</p>
        {error && <p className="mt-4 rounded-xl bg-red-50 px-4 py-3 text-sm text-red-800">{error}</p>}
        <label className="mt-5 block text-sm text-slate-600">
          Username
          <input className="mt-1 w-full rounded-xl border border-plum-100 px-3 py-2 text-ink" value={username} onChange={(event) => setUsername(event.target.value)} autoComplete="username" />
        </label>
        <label className="mt-3 block text-sm text-slate-600">
          Password
          <input className="mt-1 w-full rounded-xl border border-plum-100 px-3 py-2 text-ink" type="password" value={password} onChange={(event) => setPassword(event.target.value)} autoComplete="current-password" />
        </label>
        <button className="mt-6 w-full rounded-xl bg-plum-700 px-4 py-2.5 text-sm font-semibold text-white disabled:opacity-60" type="submit" disabled={busy}>
          {busy ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </div>
  );
}
