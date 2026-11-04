// Coalesces and paces the session-list reconciles the updates stream asks for.
// Each reconcile refetches every mounted list (both scopes and every expanded
// project folder), so letting frames start them freely stacks rounds against a
// slow server. Requests that arrive during a round collapse into one trailing
// round, and rounds are spaced further apart the longer they take; a round that
// never settles is released after a timeout.
//
// Frames also starve the lists' own polls: TanStack Query restarts a query's
// refetchInterval on every cache write, and each frame writes the list caches.
// So while frames keep arriving, a round still runs every
// RECONCILE_WHILE_ACTIVE_MS to pick up sessions no frame covers (new, or
// outside the loaded window).

/** Delay before a round, so a burst of frames triggers one. */
export const RECONCILE_DEBOUNCE_MS = 250;
/** Minimum spacing between the end of one round and the start of the next. */
export const RECONCILE_MIN_GAP_MS = 2_000;
/** Upper bound on the spacing, however slow the last round was. */
export const RECONCILE_MAX_GAP_MS = 30_000;
/**
 * A round that hasn't settled by now stops holding back the next one. Its
 * callback isn't cancelled; the next round's invalidation cancels the queries
 * it was waiting on.
 */
export const RECONCILE_ROUND_TIMEOUT_MS = 60_000;
/** Longest the lists go without a round while frames keep arriving; the Mine poll's interval. */
export const RECONCILE_WHILE_ACTIVE_MS = 60_000;

export interface ListReconcileScheduler {
  /** Ask for a reconcile; coalesced with any pending or running one. */
  schedule: () => void;
  /** Record a frame; asks for a reconcile if none started in RECONCILE_WHILE_ACTIVE_MS. */
  noteActivity: () => void;
  /** Drop a pending reconcile and stop scheduling new ones. */
  dispose: () => void;
}

/**
 * @param run - Runs one reconcile round; resolves once its refetches settle.
 * @param now - Clock, injectable for tests.
 */
export function createListReconcileScheduler(
  run: () => Promise<unknown>,
  now: () => number = Date.now,
): ListReconcileScheduler {
  let timer: ReturnType<typeof setTimeout> | null = null;
  let running = false;
  let pending = false;
  let disposed = false;
  let lastEnd = Number.NEGATIVE_INFINITY;
  let lastDuration = 0;
  // The lists were just loaded when the stream starts, so that counts as a round.
  let lastStart = now();

  const fire = async (): Promise<void> => {
    running = true;
    const started = now();
    lastStart = started;
    let roundTimer: ReturnType<typeof setTimeout> | undefined;
    try {
      await Promise.race([
        run(),
        new Promise<void>((resolve) => {
          roundTimer = setTimeout(resolve, RECONCILE_ROUND_TIMEOUT_MS);
        }),
      ]);
    } catch {
      // A failed round still counts; the next frame or poll reconciles again.
    } finally {
      clearTimeout(roundTimer);
      lastEnd = now();
      lastDuration = lastEnd - started;
      running = false;
      if (pending && !disposed) {
        pending = false;
        schedule();
      }
    }
  };

  const schedule = (): void => {
    if (disposed) return;
    if (running) {
      pending = true;
      return;
    }
    if (timer !== null) return;
    // A server that took long to answer gets proportionally fewer rounds.
    const gap = Math.min(RECONCILE_MAX_GAP_MS, Math.max(RECONCILE_MIN_GAP_MS, 2 * lastDuration));
    const delay = Math.max(RECONCILE_DEBOUNCE_MS, lastEnd + gap - now());
    timer = setTimeout(() => {
      timer = null;
      void fire();
    }, delay);
  };

  return {
    schedule,
    noteActivity() {
      if (now() - lastStart >= RECONCILE_WHILE_ACTIVE_MS) schedule();
    },
    dispose() {
      disposed = true;
      pending = false;
      if (timer !== null) clearTimeout(timer);
      timer = null;
    },
  };
}
