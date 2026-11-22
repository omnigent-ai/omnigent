// Most-recently-visited order for ⌃] / ⌃[, like a tab switcher: a
// visit moves the session to the front (nothing is truncated), and a switch
// walks a snapshot of that order taken when the gesture starts.

/** How many visits are remembered; older ones fall off the end. */
export const RECENT_SESSION_HISTORY_LIMIT = 50;

/** One step toward older visits (1, ⌃]) or back toward newer ones (-1, ⌃[). */
export type RecentSessionDirection = 1 | -1;

/** The history after visiting `sessionId`: moved to the front, deduped, capped. */
export function recordSessionVisit(
  history: readonly string[],
  sessionId: string,
  limit = RECENT_SESSION_HISTORY_LIMIT,
): string[] {
  return [sessionId, ...history.filter((id) => id !== sessionId)].slice(0, limit);
}

/**
 * The order a switch walks: visited sessions that are still live, most recent
 * first, then the never-visited ones in sidebar order. A visit the sidebar hides
 * (a collapsed folder, another filter) stays reachable; deleted and archived
 * sessions drop out because `isLive` rejects them.
 */
export function recentSessionOrder(
  history: readonly string[],
  sidebarOrderIds: readonly string[],
  isLive: (sessionId: string) => boolean,
): string[] {
  const seen = new Set<string>();
  const order: string[] = [];
  for (const id of [...history, ...sidebarOrderIds]) {
    if (isLive(id) && !seen.has(id)) {
      seen.add(id);
      order.push(id);
    }
  }
  return order;
}

/**
 * The index one step from `fromIndex`, wrapping at both ends and passing over
 * entries that are no longer eligible. Undefined when none is.
 */
export function steppedRecentIndex(
  order: readonly string[],
  fromIndex: number,
  direction: RecentSessionDirection,
  isEligible: (sessionId: string) => boolean = () => true,
): number | undefined {
  for (let step = 1; step <= order.length; step++) {
    const index = (((fromIndex + direction * step) % order.length) + order.length) % order.length;
    if (isEligible(order[index]!)) return index;
  }
  return undefined;
}

// Persisted per device, so ⌃] still knows the last
// session after a reload or relaunch.
const STORAGE_KEY = "omnigent:recent-session-history";

/** The persisted visit history; empty when missing, malformed, or unreadable. */
export function readRecentSessionHistory(): string[] {
  if (typeof window === "undefined") return [];
  try {
    const parsed: unknown = JSON.parse(window.localStorage.getItem(STORAGE_KEY) ?? "[]");
    if (!Array.isArray(parsed)) return [];
    return parsed
      .filter((id): id is string => typeof id === "string")
      .slice(0, RECENT_SESSION_HISTORY_LIMIT);
  } catch {
    return [];
  }
}

/** Persist the visit history, ignoring quota and access errors. */
export function writeRecentSessionHistory(history: readonly string[]): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(history));
  } catch {
    // A failed write only costs the history after the next reload.
  }
}
