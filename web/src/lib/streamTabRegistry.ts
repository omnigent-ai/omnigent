// Counts how many same-origin tabs currently hold a session event stream.
//
// Each open conversation holds one long-lived `GET /v1/sessions/{id}/stream`
// SSE request for as long as it's bound. Browsers cap HTTP/1.1 connections at
// ~6 per origin and that budget is shared across every tab in the profile, so
// once ~6 conversations are open in parallel the held streams occupy every
// slot and unrelated requests (navigation, API calls) queue behind them — the
// UI appears hung. This module measures that pressure so the app can warn
// instead of silently stalling.
//
// Web Locks, not heartbeats: a lock is released by the browser automatically
// when its tab goes away, including a crash or force-quit. A
// localStorage/BroadcastChannel heartbeat would need an expiry window and
// would still report phantom tabs for seconds after a crash.
//
// Scope: `navigator.locks` spans same-origin contexts within ONE browser
// profile. A second browser, or a separate profile / incognito window, is
// invisible here — which is correct, because the connection pool it competes
// for is also per-profile.
//
// Counting is per BOUND STREAM, not per tab: a tab sitting on the sidebar,
// settings, or inbox holds no stream and consumes no slot, so it must not
// inflate the count. `startStreamPump` owns acquire/release for exactly the
// window in which its stream exists.

/** Lock-name prefix identifying a held session event stream. */
const LOCK_PREFIX = "omnigent.stream.";

/**
 * How often to re-read the lock table while something is observing the count.
 *
 * Tabs open and close on human timescales and the count only drives an
 * advisory banner, so a slow poll is ample. `navigator.locks.query()` is
 * async, so a synchronous `getSnapshot` (what `useSyncExternalStore` needs)
 * has to read a cached value that this poll refreshes.
 */
const POLL_INTERVAL_MS = 3_000;

type Listener = () => void;

const listeners = new Set<Listener>();
let cachedCount = 0;
let pollTimer: ReturnType<typeof setInterval> | null = null;
/** Guards against overlapping queries if one poll outlives its interval. */
let querying = false;
/** A refresh arrived mid-query; re-run once the in-flight one finishes. */
let refreshPending = false;

/** Whether this browser exposes the Web Locks API (absent on older Safari). */
function locksAvailable(): boolean {
  return typeof navigator !== "undefined" && navigator.locks !== undefined;
}

/**
 * Whether the origin was reached over HTTP/1.1, where the ~6-connection cap
 * that makes held SSE streams dangerous actually applies.
 *
 * HTTP/2 and HTTP/3 multiplex every request over one connection, so N held
 * streams cost N cheap streams rather than N of 6 sockets and there is nothing
 * to warn about. That is the shape of the Databricks Apps deployment (its
 * ingress is what imposes the ~5-min HTTP/2 stream cap the pump reconnects
 * around), while local `uvicorn` and plain reverse proxies serve HTTP/1.1.
 *
 * Read from the navigation timing entry's ALPN identifier. An unknown or empty
 * value (some proxies omit it; older browsers lack the field) is treated as
 * HTTP/1.1 so the warning fails toward being shown rather than silently
 * suppressed on the very setups most likely to stall.
 *
 * @returns `true` when the banner's premise holds for this page load.
 */
export function connectionHasLowStreamLimit(): boolean {
  if (typeof performance === "undefined") return true;
  const [nav] = performance.getEntriesByType("navigation") as PerformanceNavigationTiming[];
  const protocol = nav?.nextHopProtocol;
  if (!protocol) return true;
  // ALPN ids: "http/1.0", "http/1.1", "h2", "h2c", "h3", "h3-29", …
  return protocol.startsWith("http/1");
}

/**
 * Hold a lock naming this tab's session event stream until released.
 *
 * The lock name is unique per call, so locks from different tabs never
 * contend — every one is granted immediately and shows up in
 * {@link navigator.LockManager.query}. A shared name would serialize them and
 * the count would always read 1.
 *
 * Safe to call when Web Locks is unavailable: the returned release function is
 * a no-op and the count simply stays 0 (the banner never appears, which is the
 * right failure mode for an advisory warning).
 *
 * @returns A release function. Idempotent; call it when the stream ends.
 */
export function acquireStreamSlot(): () => void {
  if (!locksAvailable()) return () => {};

  const name = `${LOCK_PREFIX}${crypto.randomUUID()}`;
  let release: (() => void) | null = null;
  // The lock is held for as long as the callback's promise is pending, so
  // resolving `held` is what releases it. An AbortSignal would only cancel a
  // still-PENDING request, which is not what we need here.
  const held = new Promise<void>((resolve) => {
    release = resolve;
  });

  void navigator.locks
    .request(name, () => held)
    .catch(() => {
      // A rejected request means the lock was never held, so there is nothing
      // to release and nothing to count. Stay silent: this is advisory only.
    });

  // Refresh promptly so a newly-bound stream is reflected without waiting a
  // whole poll interval.
  void refreshCount();

  return () => {
    release?.();
    release = null;
    // The browser drops the lock only after the held promise settles, so an
    // immediate re-query would still observe it. Defer past the microtask queue
    // so the refresh sees the post-release table.
    setTimeout(() => void refreshCount(), 0);
  };
}

/**
 * Re-read the lock table and notify listeners when the count changed.
 *
 * Failures are swallowed: a browser that rejects `query()` leaves the last
 * known count in place rather than flapping the banner.
 */
async function refreshCount(): Promise<void> {
  if (!locksAvailable()) return;
  if (querying) {
    // Coalesce rather than drop: a slot acquired/released while a query is in
    // flight must still be observed, or the count would sit stale until the
    // next poll tick.
    refreshPending = true;
    return;
  }
  querying = true;
  try {
    const { held = [] } = await navigator.locks.query();
    const next = held.filter((lock) => lock.name?.startsWith(LOCK_PREFIX)).length;
    if (next !== cachedCount) {
      cachedCount = next;
      for (const listener of listeners) listener();
    }
  } catch {
    // Keep the previous value.
  } finally {
    querying = false;
  }
  if (refreshPending) {
    refreshPending = false;
    await refreshCount();
  }
}

function startPolling(): void {
  if (pollTimer !== null || !locksAvailable()) return;
  void refreshCount();
  pollTimer = setInterval(() => {
    // Only a visible tab can show the banner, so don't spend queries while
    // hidden. Returning to the tab re-reads immediately via the listener below.
    if (typeof document !== "undefined" && document.hidden) return;
    void refreshCount();
  }, POLL_INTERVAL_MS);
}

function stopPolling(): void {
  if (pollTimer === null) return;
  clearInterval(pollTimer);
  pollTimer = null;
}

/** Re-read on focus so a tab returning to the foreground isn't stale. */
function onVisibilityChange(): void {
  if (!document.hidden) void refreshCount();
}

/**
 * Subscribe to the number of same-origin tabs holding a session event stream.
 *
 * Polling runs only while at least one subscriber is listening, so an app that
 * never renders the banner pays nothing.
 *
 * @param listener - Called whenever the count changes.
 * @returns An unsubscribe function.
 */
export function subscribeStreamTabCount(listener: Listener): () => void {
  listeners.add(listener);
  if (listeners.size === 1) {
    startPolling();
    if (typeof document !== "undefined") {
      document.addEventListener("visibilitychange", onVisibilityChange);
    }
  }
  return () => {
    listeners.delete(listener);
    if (listeners.size === 0) {
      stopPolling();
      if (typeof document !== "undefined") {
        document.removeEventListener("visibilitychange", onVisibilityChange);
      }
    }
  };
}

/**
 * The last observed number of stream-holding tabs.
 *
 * Synchronous by design (`useSyncExternalStore` requires it) and therefore
 * eventually consistent — up to {@link POLL_INTERVAL_MS} stale. Reads 0 where
 * Web Locks is unavailable.
 *
 * @returns The cached count, including this tab's own stream.
 */
export function getStreamTabCount(): number {
  return cachedCount;
}

/** Reset module state. Test-only seam so cases don't leak into each other. */
export function resetStreamTabRegistryForTests(): void {
  listeners.clear();
  stopPolling();
  cachedCount = 0;
  querying = false;
  refreshPending = false;
}
