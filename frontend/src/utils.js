export function calculateAttendance(fullDays, halfDays, totalWorkingDays) {
  const full = Number(fullDays);
  const half = Number(halfDays);
  const total = Number(totalWorkingDays);
  if (![full, half, total].every((value) => Number.isFinite(value) && value >= 0)) return 0;
  if (total <= 0) return 0;
  const credits = Math.min(full + half * 0.5, total);
  return Number(((credits / total) * 100).toFixed(2));
}

export function percent(value) {
  if (value === null || value === undefined || value === "") return "—";
  const number = Number(value);
  if (!Number.isFinite(number)) return "—";
  return `${number.toFixed(2)}%`;
}

export function shortDate(iso) {
  if (!iso) return "—";
  const [year, month, day] = iso.split("-").map(Number);
  return new Date(Date.UTC(year, month - 1, day)).toLocaleDateString("en-GB", {
    day: "2-digit",
    month: "short",
    timeZone: "UTC",
  });
}

export function longDate(iso) {
  if (!iso) return "—";
  const [year, month, day] = iso.split("-").map(Number);
  return new Date(Date.UTC(year, month - 1, day)).toLocaleDateString("en-GB", {
    day: "2-digit",
    month: "short",
    year: "numeric",
    timeZone: "UTC",
  });
}

export function stamp(value) {
  if (!value) return "Never";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleString("en-IN", { dateStyle: "medium", timeStyle: "short" });
}

export function hours(minutes) {
  if (minutes === null || minutes === undefined || minutes === "") return "—";
  const whole = Math.floor(Math.abs(minutes) / 60);
  const rest = Math.abs(minutes) % 60;
  return `${whole}h ${String(rest).padStart(2, "0")}m`;
}
