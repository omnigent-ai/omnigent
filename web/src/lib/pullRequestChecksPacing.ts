// How often an open PR's panel reads it again while its checks run or haven't appeared: fast
// right after it opens, a push or a check change, then slower, and never stopping, since no
// checks yet can't tell a repo without CI from checks that haven't registered.

/** Watching checks reads every 5 s this long after the PR opens or its head or checks change. */
export const CHECKS_FAST_PHASE_MS = 2 * 60_000;
export const CHECKS_FAST_POLL_MS = 5_000;
export const CHECKS_SLOW_POLL_MS = 30_000;
/** With still no checks this long after the last change, it only looks for them now and then. */
export const NO_CHECKS_DISCOVERY_AFTER_MS = 10 * 60_000;
export const NO_CHECKS_DISCOVERY_POLL_MS = 2 * 60_000;

/** The flag that rolls the pacing back to a steady 5 s poll. */
export const PULL_REQUEST_CHECKS_PACING_FLAG = "pull-request-checks-pacing";

/** The poll while watching checks, `sinceChangeMs` after the PR opened or last changed. */
export function pacedChecksPollInterval(sinceChangeMs: number, noChecksYet: boolean): number {
  if (sinceChangeMs < CHECKS_FAST_PHASE_MS) return CHECKS_FAST_POLL_MS;
  if (noChecksYet && sinceChangeMs >= NO_CHECKS_DISCOVERY_AFTER_MS) {
    return NO_CHECKS_DISCOVERY_POLL_MS;
  }
  return CHECKS_SLOW_POLL_MS;
}

interface ChecksState {
  head: string | undefined;
  checks: unknown;
}

interface SeenChecks {
  signature: string;
  changedAt: number;
}

// Per query, the head and checks its answers last showed, and when they last changed.
const seen = new WeakMap<object, SeenChecks>();

/**
 * When `query`'s PR last changed its head or checks, as of `now`. An answer that shows the
 * same head and checks as the one before leaves it where it was.
 */
export function checksChangedAt(query: object, pr: ChecksState, now: number): number {
  const signature = JSON.stringify([pr.head ?? null, pr.checks ?? null]);
  const previous = seen.get(query);
  if (previous?.signature === signature) return previous.changedAt;
  seen.set(query, { signature, changedAt: now });
  return now;
}
