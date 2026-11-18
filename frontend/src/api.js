// Hosted Vercel frontend talks to Railway. Local :8000 must stay same-origin.
const DEFAULT_PROD_API = "https://kk-attendance-production.up.railway.app";

function resolveApiBase() {
  // Admin Mac / local API — always same-origin, even if .env.production points at Railway.
  if (typeof window !== "undefined") {
    const host = window.location.hostname;
    if (host === "localhost" || host === "127.0.0.1") return "";
  }
  const fromEnv = (import.meta.env.VITE_API_URL || "").replace(/\/$/, "");
  if (fromEnv) return fromEnv;
  if (import.meta.env.PROD) return DEFAULT_PROD_API;
  return "";
}

export function apiUrl(path) {
  const base = resolveApiBase();
  if (!path) return base || "/";
  if (/^https?:\/\//i.test(path)) return path;
  return `${base}${path.startsWith("/") ? path : `/${path}`}`;
}

export async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  const base = resolveApiBase();
  if (base.includes("ngrok")) {
    headers["ngrok-skip-browser-warning"] = "1";
  }
  if (options.body && !(options.body instanceof FormData) && !headers["Content-Type"]) {
    headers["Content-Type"] = "application/json";
  }
  let response;
  try {
    response = await fetch(apiUrl(path), { ...options, headers, credentials: "include" });
  } catch {
    throw new Error("Cannot reach the API. Start the local backend on port 8000, then try again.");
  }
  const type = response.headers.get("content-type") || "";
  if (!response.ok) {
    let message = response.statusText || "Request failed";
    if (response.status === 500 && message === "Internal Server Error" && !base) {
      message = "Local API is not running on port 8000. Start the backend, then sign in again.";
    }
    if (type.includes("json")) {
      try {
        const data = await response.json();
        if (typeof data.detail === "string") message = data.detail;
      } catch {
        /* keep message */
      }
    }
    const error = new Error(message);
    error.status = response.status;
    throw error;
  }
  if (type.includes("json")) return response.json();
  return response;
}
