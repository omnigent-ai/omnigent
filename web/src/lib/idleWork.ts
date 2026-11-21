// Background work split into slices that run while the browser is idle, so it
// never delays input or a frame the user is waiting on.

/** Time each slice may take where the browser can't report idle time (Safari). */
const FALLBACK_SLICE_MS = 5;
/** Longest a slice waits for idle time before running anyway. */
const IDLE_TIMEOUT_MS = 1_000;

/**
 * Call `step` in idle slices until it returns true. `step` receives the time
 * left in its slice and should stop when it runs low. Returns a function that
 * cancels the remaining slices.
 */
export function runInIdleSlices(step: (timeLeft: () => number) => boolean): () => void {
  let cancelled = false;
  let cancelPending = () => {};
  const schedule = () => {
    if (cancelled) return;
    if (typeof globalThis.requestIdleCallback === "function") {
      const handle = globalThis.requestIdleCallback(
        (deadline) => {
          if (!cancelled && !step(() => deadline.timeRemaining())) schedule();
        },
        { timeout: IDLE_TIMEOUT_MS },
      );
      cancelPending = () => globalThis.cancelIdleCallback(handle);
    } else {
      const handle = setTimeout(() => {
        const sliceEnd = performance.now() + FALLBACK_SLICE_MS;
        if (!cancelled && !step(() => sliceEnd - performance.now())) schedule();
      }, 0);
      cancelPending = () => clearTimeout(handle);
    }
  };
  schedule();
  return () => {
    cancelled = true;
    cancelPending();
  };
}
