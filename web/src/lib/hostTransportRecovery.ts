// When a session's host doesn't answer a read at all, as while a resumed machine
// starts, the read is retried as one bounded episode. Episodes outlive each fetch,
// so polling, focus, remounts and invalidations can't start a new chain once one
// is used up; only an explicit retry, a success or the host coming back can.

/** The request never reached an answer: the network or the session's machine is down. */
export class HostUnreachableError extends Error {
  constructor(cause?: unknown) {
    super("Couldn’t reach this session’s machine.", { cause });
    this.name = "HostUnreachableError";
  }
}

/** Gateway answers that mean the server couldn't get one from the host. */
const GATEWAY_STATUSES = new Set([502, 503, 504]);

/**
 * Whether `error` says the host didn't answer, rather than answering with a
 * failure: an HTTP error carrying a gateway status, or {@link HostUnreachableError}.
 * Callers turn a `runner_unavailable` 503 into their own offline error first.
 */
export function isHostTransportError(error: unknown): boolean {
  if (error instanceof HostUnreachableError) return true;
  if (!(error instanceof Error) || !("status" in error)) return false;
  return typeof error.status === "number" && GATEWAY_STATUSES.has(error.status);
}

/** Retries after the first failed attempt, so an episode makes seven attempts. */
export const HOST_TRANSPORT_RETRIES = 6;
const RETRY_DELAYS_MS = [1_000, 2_000, 4_000, 8_000, 15_000, 15_000] as const;

/** The wait before retry `retryNumber` (1-based) of an episode. */
export function hostTransportRetryDelay(retryNumber: number): number {
  return RETRY_DELAYS_MS[Math.min(Math.max(retryNumber, 1), RETRY_DELAYS_MS.length) - 1];
}

interface Episode {
  sessionId: string;
  failures: number;
  lastError: unknown;
}

const episodes = new Map<string, Episode>();

/** Count one failed attempt of the read `key`; returns the episode's failures so far. */
export function recordHostTransportFailure(key: string, sessionId: string, error: unknown): number {
  const episode = episodes.get(key) ?? { sessionId, failures: 0, lastError: error };
  episode.failures += 1;
  episode.lastError = error;
  episodes.set(key, episode);
  return episode.failures;
}

/** Failed attempts in the read's current episode. */
export function hostTransportFailures(key: string): number {
  return episodes.get(key)?.failures ?? 0;
}

/** The error that used up the read's episode, or undefined while attempts remain. */
export function exhaustedHostTransportError(key: string): unknown {
  const episode = episodes.get(key);
  return episode && episode.failures > HOST_TRANSPORT_RETRIES ? episode.lastError : undefined;
}

/** Whether the read may retry after a failure counted into its episode. */
export function mayRetryHostTransport(key: string): boolean {
  const failures = hostTransportFailures(key);
  return failures > 0 && failures <= HOST_TRANSPORT_RETRIES;
}

/** End the read's episode: it succeeded, or someone asked to try again. */
export function clearHostTransportRecovery(key: string): void {
  episodes.delete(key);
}

/** End every episode for the session's reads, as when its host comes back. */
export function clearSessionHostTransportRecovery(sessionId: string): void {
  for (const [key, episode] of episodes) {
    if (episode.sessionId === sessionId) episodes.delete(key);
  }
}

const readGates = new Map<string, boolean>();

/** Publish whether the session's host may be asked now; every observer reports the same value. */
export function setHostReadGate(sessionId: string, open: boolean): void {
  readGates.set(sessionId, open);
}

/** Whether the session's host may be asked now; unknown sessions may be. */
export function hostReadGateOpen(sessionId: string): boolean {
  return readGates.get(sessionId) !== false;
}

/** Test seam: forget every episode and gate. */
export function resetHostTransportRecovery(): void {
  episodes.clear();
  readGates.clear();
}
