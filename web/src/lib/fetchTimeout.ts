/**
 * Bounded fetch helper.
 *
 * Wrap a one-shot request in a wall-clock deadline so a backend that
 * accepts but never answers (wedged proxy, stuck runner tunnel) settles
 * instead of leaving the caller suspended indefinitely.
 *
 * NOT for streaming / long-held endpoints (SSE pumps, event-send POST,
 * WebSocket upgrade). Those legitimately hold connections open and must
 * not be wrapped here.
 */

export const DEFAULT_API_TIMEOUT_MS = 30_000;

/**
 * Deadline for session-create / runner-launch POSTs.
 *
 * These mutations can synchronously run a host `git worktree add`
 * (server budget `_WORKTREE_TIMEOUT_S` = 150 s) plus host launch (~30 s)
 * and runner init (~10 s). The client deadline must sit ABOVE the
 * server's worst case so it only fires when the server is genuinely
 * wedged (never responds) — not on a slow-but-valid create.
 */
export const SESSION_MUTATION_TIMEOUT_MS = 240_000;

/** Thrown when `fetchWithTimeout` fires its deadline before `run` settles. */
export class ApiTimeoutError extends Error {
  constructor(timeoutMs: number) {
    super(`Request timed out after ${timeoutMs}ms`);
    this.name = "ApiTimeoutError";
  }
}

/**
 * Run a fetch within a bounded wall-clock deadline.
 *
 * Races `run(signal)` against a rejecting timer so the returned promise
 * always settles within `timeoutMs`, even if the underlying transport
 * ignores the abort signal. `controller.abort()` is called on timeout as
 * best-effort request cancellation for transports that honour it (plain
 * `fetch` does). The timer is always cleared in a `finally` block.
 *
 * Pass `externalSignal` to also abort the internal controller (and therefore
 * `run`) when a caller-owned signal fires — e.g. react-query's per-query
 * cancellation signal. Composed manually instead of `AbortSignal.any` to
 * keep browser-support requirements conservative.
 */
export function fetchWithTimeout<T>(
  run: (signal: AbortSignal) => Promise<T>,
  timeoutMs: number = DEFAULT_API_TIMEOUT_MS,
  externalSignal?: AbortSignal,
): Promise<T> {
  const controller = new AbortController();
  if (externalSignal) {
    if (externalSignal.aborted) {
      controller.abort();
    } else {
      externalSignal.addEventListener("abort", () => controller.abort(), { once: true });
    }
  }
  let timer: ReturnType<typeof setTimeout> | undefined;
  const timeoutPromise = new Promise<never>((_, reject) => {
    timer = setTimeout(() => {
      controller.abort();
      reject(new ApiTimeoutError(timeoutMs));
    }, timeoutMs);
  });
  return Promise.race([run(controller.signal), timeoutPromise]).finally(() => {
    clearTimeout(timer);
  });
}
