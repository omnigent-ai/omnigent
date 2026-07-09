const MIN_MS = 60_000;
const HOUR_MS = 60 * MIN_MS;
const DAY_MS = 24 * HOUR_MS;
const WEEK_MS = 7 * DAY_MS;
const MONTH_MS = 30 * DAY_MS;
const YEAR_MS = 365 * DAY_MS;

// "mo" (not "m") for months disambiguates from minutes.
export function relativeTime(timestampMs: number, nowMs: number = Date.now()): string {
  const diff = Math.max(0, nowMs - timestampMs);
  if (diff < MIN_MS) return "now";
  if (diff < HOUR_MS) return `${Math.floor(diff / MIN_MS)}m`;
  if (diff < DAY_MS) return `${Math.floor(diff / HOUR_MS)}h`;
  if (diff < WEEK_MS) return `${Math.floor(diff / DAY_MS)}d`;
  if (diff < MONTH_MS) return `${Math.floor(diff / WEEK_MS)}w`;
  if (diff < YEAR_MS) return `${Math.floor(diff / MONTH_MS)}mo`;
  return `${Math.floor(diff / YEAR_MS)}y`;
}

export function absoluteTime(timestampMs: number): string {
  return new Date(timestampMs).toLocaleString();
}
