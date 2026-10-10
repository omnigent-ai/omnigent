// Reconnect pacing for the session SSE stream pump (chatStore.ts). Split out
// so the delay/backoff logic is unit-testable without the store's dependency
// graph. The backoff policy itself is shared with sessionUpdatesSocket.ts
// (see reconnectBackoff.ts): 250 ms base, doubling, capped at 5 s visible /
// 60 s hidden, ±50% jitter.
//
// Databricks Apps' ingress hard-caps a single HTTP/2 stream at ~5 min, so the
// client must re-subscribe when it's dropped. Backoff applies only between
// consecutive failed opens (see nextReconnectDelay); a drop after a healthy
// connection reconnects instantly.
export {
  HIDDEN_RECONNECT_MAX_MS,
  RECONNECT_MAX_MS,
  nextReconnectDelay,
} from "@/lib/reconnectBackoff";

// Spread background reconnects over a few seconds so N conversations recycling
// at the same ingress deadline don't fire N snapshot fetches at once. Small
// enough that a backgrounded conversation is still current well before the user
// could switch to it.
export const BACKGROUND_RECONNECT_JITTER_MAX_MS = 3_000;

export function backgroundReconnectJitter(): number {
  return Math.random() * BACKGROUND_RECONNECT_JITTER_MAX_MS;
}

/**
 * Resolve after `ms`, or immediately when `signal` aborts (so switchTo /
 * unmount interrupts a pending reconnect backoff instead of stalling the
 * loop's teardown).
 */
export function abortableDelay(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    const onAbort = (): void => {
      clearTimeout(timer);
      signal.removeEventListener("abort", onAbort);
      resolve();
    };
    const timer = setTimeout(() => {
      signal.removeEventListener("abort", onAbort);
      resolve();
    }, ms);
    signal.addEventListener("abort", onAbort, { once: true });
    // Register first, then re-check: an abort that fired before the listener
    // was attached won't dispatch to it, so resolve now if already aborted.
    // (`resolve` is idempotent; this closes any registration-ordering gap.)
    if (signal.aborted) onAbort();
  });
}

/**
 * Wait out a reconnect delay. Any delay scheduled while the page is hidden is
 * raced against a visibility flip, so returning to the tab reconnects promptly
 * — for stretched waits (up to 60 s) but also for short sampled ones, where a
 * plain timer would let a just-foregrounded tab sit out the remainder.
 * `wakeJitterMs` staggers that wake (0 = immediate): every stretched wait in
 * the tab resolves on the same visibilitychange, so background conversations
 * add jitter — the same herd-avoidance as backgroundReconnectJitter(). A page
 * re-hidden before the jittered wake fires resumes the remaining stretched
 * wait instead of reconnecting while hidden.
 */
export async function awaitReconnectDelay(
  ms: number,
  signal: AbortSignal,
  wakeJitterMs: () => number = () => 0,
): Promise<void> {
  // Visible pages (and non-DOM environments) have no visibility wake to wait
  // for — a plain abortable timer suffices. EVERY hidden-page delay takes the
  // visibility-aware path, regardless of length: gating on ms > cap would let
  // a hidden-sampled delay ≤ 5 s keep the page disconnected after it becomes
  // visible mid-wait.
  if (typeof document === "undefined" || !document.hidden) {
    return abortableDelay(ms, signal);
  }
  return new Promise<void>((resolve) => {
    const deadline = Date.now() + ms;
    const done = () => {
      clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVisibilityChange);
      signal.removeEventListener("abort", done);
      resolve();
    };
    const fireIfVisible = () => {
      const remaining = deadline - Date.now();
      // The wake jitter can expire after the page re-hid (visible → hidden
      // within the jitter window): resume the remaining stretched wait
      // instead of reconnecting while hidden.
      if (document.hidden && remaining > 0) {
        timer = setTimeout(fireIfVisible, remaining);
        return;
      }
      done();
    };
    const onVisibilityChange = () => {
      if (document.hidden) return;
      clearTimeout(timer);
      timer = setTimeout(fireIfVisible, wakeJitterMs());
    };
    let timer = setTimeout(done, ms);
    document.addEventListener("visibilitychange", onVisibilityChange);
    signal.addEventListener("abort", done, { once: true });
    // Same guard as abortableDelay: an abort that fired before the listener
    // attached never dispatches, and would otherwise wait out the full
    // stretched delay.
    if (signal.aborted) done();
  });
}
