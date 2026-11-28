// How old the pull request data a surface shows is, while it shows data kept on this device:
// "Updated 12 min ago", and whether GitHub is being read again behind it. One model for every
// surface that shows the shared freshness mark, so their words can't drift.

const MINUTE_MS = 60_000;
const HOUR_MS = 60 * MINUTE_MS;
const DAY_MS = 24 * HOUR_MS;

/** One read a surface shows, as its query reports it. */
export interface PullRequestFreshnessSource {
  /** When GitHub answered the shown data, in ms; 0 or less while there's none. */
  updatedAt: number;
  /** The shown data came from this device rather than this run's read. */
  restored: boolean;
  /** GitHub is being read again. */
  refreshing: boolean;
  /** The last read again didn't go through. */
  failed: boolean;
}

/** What the mark shows: the oldest kept data on the surface, or nothing when all of it is fresh. */
export interface PullRequestFreshnessState {
  updatedAt: number;
  refreshing: boolean;
  failed: boolean;
}

/**
 * The surface's freshness from the reads it shows. Only kept data counts, and the oldest of it
 * names the age, so the mark goes once GitHub has answered every read again.
 */
export function pullRequestFreshness(
  sources: readonly PullRequestFreshnessSource[],
): PullRequestFreshnessState | null {
  const kept = sources.filter((source) => source.restored && source.updatedAt > 0);
  if (kept.length === 0) return null;
  return {
    updatedAt: Math.min(...kept.map((source) => source.updatedAt)),
    refreshing: kept.some((source) => source.refreshing),
    failed: kept.some((source) => source.failed),
  };
}

/**
 * "12 min ago", "3 hr ago", "yesterday", or a date, for data GitHub answered at `updatedAt`.
 * Null under a minute, and for a time ahead of this Mac's clock, where no age is worth showing.
 */
export function freshnessAge(updatedAt: number, now: number): string | null {
  if (!Number.isFinite(updatedAt) || updatedAt <= 0) return null;
  const age = now - updatedAt;
  if (age < MINUTE_MS) return null;
  if (age < HOUR_MS) return `${Math.floor(age / MINUTE_MS)} min ago`;
  if (age < DAY_MS) return `${Math.floor(age / HOUR_MS)} hr ago`;
  const then = new Date(updatedAt);
  const yesterday = new Date(now);
  yesterday.setDate(yesterday.getDate() - 1);
  if (then.toDateString() === yesterday.toDateString()) return "yesterday";
  const sameYear = then.getFullYear() === new Date(now).getFullYear();
  return `on ${then.toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
    ...(sameYear ? {} : { year: "numeric" }),
  })}`;
}

/**
 * The mark's words, with the exact time for its tooltip, or null where it shows nothing. `age`
 * alone is what a narrow surface shows.
 */
export function freshnessLabel(
  state: PullRequestFreshnessState | null,
  now: number,
): { age: string; text: string; status: string | null; exact: string } | null {
  if (!state) return null;
  const age = freshnessAge(state.updatedAt, now);
  if (!age) return null;
  return {
    age,
    text: `Updated ${age}`,
    status: state.refreshing ? "refreshing" : state.failed ? "couldn't refresh" : null,
    exact: `Updated ${new Date(state.updatedAt).toLocaleString()}`,
  };
}
