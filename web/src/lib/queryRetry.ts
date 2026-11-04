import { WRONG_REPLICA_CODE } from "@/lib/identity";
import { ApiError } from "@/lib/sessionsApi";

// Pacing for automatic query retries. The delay doubles from 1 s up to 30 s and
// is jittered to 50–150% of that, so clients that failed together (a pod
// restart, a throttling burst) don't come back together. A server's
// Retry-After wins.

export const QUERY_RETRY_BASE_MS = 1_000;
export const QUERY_RETRY_MAX_MS = 30_000;
/** Longest Retry-After honored; past it, the query's next poll takes over. */
export const QUERY_RETRY_AFTER_MAX_MS = 60_000;

// Client errors that a retry can fix: request timeout, too early, throttled.
const RETRYABLE_CLIENT_STATUSES = new Set([408, 425, 429]);

/**
 * Whether repeating a failed request can succeed.
 *
 * @param error - The query's error.
 * @returns `false` for a client error the same request will hit again (bad
 *   input, missing session, no access); `true` for 5xx, throttling,
 *   `wrong_replica`, network failures, and errors without a status.
 */
export function isRetryableRequestError(error: unknown): boolean {
  if (error instanceof ApiError) {
    // A 400 `wrong_replica` is routing, not the request: it clears once Dicer
    // finishes reassigning keys, e.g. while pods roll.
    if (error.code === WRONG_REPLICA_CODE) return true;
    return error.status >= 500 || RETRYABLE_CLIENT_STATUSES.has(error.status);
  }
  return true;
}

/**
 * Delay before retry number `failureCount + 1`; the default `retryDelay` for
 * every query.
 *
 * @param failureCount - Failures so far, starting at 0.
 * @param error - The failure, whose Retry-After takes precedence.
 * @returns The jittered delay in milliseconds.
 */
export function queryRetryDelay(failureCount: number, error: unknown): number {
  if (error instanceof ApiError && error.retryAfterMs !== null) {
    return Math.min(error.retryAfterMs, QUERY_RETRY_AFTER_MAX_MS);
  }
  const base = Math.min(QUERY_RETRY_BASE_MS * 2 ** failureCount, QUERY_RETRY_MAX_MS);
  return base / 2 + Math.random() * base;
}
