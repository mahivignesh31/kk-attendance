// Production default: Railway API (so Git-imported Vercel builds work without env).
const DEFAULT_PROD_API = "https://kk-attendance-production.up.railway.app";
const API_BASE = (
  import.meta.env.VITE_API_URL ||
  (import.meta.env.PROD ? DEFAULT_PROD_API : "")
).replace(/\/$/, "");

export function apiUrl(path) {
  if (!path) return API_BASE || "/";
  if (/^https?:\/\//i.test(path)) return path;
  return `${API_BASE}${path.startsWith("/") ? path : `/${path}`}`;
}

export async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (API_BASE.includes("ngrok")) {
    headers["ngrok-skip-browser-warning"] = "1";
  }
  if (options.body && !(options.body instanceof FormData) && !headers["Content-Type"]) {
    headers["Content-Type"] = "application/json";
  }
  const response = await fetch(apiUrl(path), { ...options, headers, credentials: "include" });
  const type = response.headers.get("content-type") || "";
  if (!response.ok) {
    let message = response.statusText || "Request failed";
    if (type.includes("json")) {
      const data = await response.json();
      if (typeof data.detail === "string") message = data.detail;
    }
    const error = new Error(message);
    error.status = response.status;
    throw error;
  }
  if (type.includes("json")) return response.json();
  return response;
}
