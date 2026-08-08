// Wall-clock label for a transcript message ("3:42 PM", "Yesterday 3:42 PM",
// "Mar 4, 3:42 PM"). Absolute rather than relative: a transcript is read
// top-to-bottom as a record, so "5m ago" would go stale line-by-line while
// an absolute stamp stays true. Locale-aware via `toLocale*String`.

/**
 * Format a message's server creation time for display next to its bubble.
 *
 * - Same calendar day as `now`: time only ("3:42 PM").
 * - The day before: "Yesterday 3:42 PM".
 * - Same year: "Mar 4, 3:42 PM".
 * - Older: "Mar 4, 2025, 3:42 PM".
 *
 * @param createdAtS - Server creation time in unix epoch seconds.
 * @param now - The current instant (injectable for tests and for callers
 *   driving freshness from the shared `useNow()` tick).
 * @returns The label, or `""` for a non-finite or non-positive stamp so a
 *   malformed item renders nothing rather than "Invalid Date".
 */
export function formatMessageTime(createdAtS: number, now: Date = new Date()): string {
  if (!Number.isFinite(createdAtS) || createdAtS <= 0) return "";
  const date = new Date(createdAtS * 1000);
  const time = date.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
  if (date.toDateString() === now.toDateString()) return time;
  const yesterday = new Date(now);
  yesterday.setDate(now.getDate() - 1);
  if (date.toDateString() === yesterday.toDateString()) return `Yesterday ${time}`;
  const day = date.toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
    ...(date.getFullYear() !== now.getFullYear() ? { year: "numeric" } : {}),
  });
  return `${day}, ${time}`;
}
