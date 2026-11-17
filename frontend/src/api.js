export async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (options.body && !(options.body instanceof FormData) && !headers["Content-Type"]) {
    headers["Content-Type"] = "application/json";
  }
  const response = await fetch(path, { ...options, headers, credentials: "include" });
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
