// Coalesces and paces the session-list reconciles the updates stream asks for.
// A reconcile refetches either every mounted list (both scopes and every
// expanded project folder) or only the lists a frame named, so letting frames
// start them freely stacks rounds against a slow server. Requests that arrive
// before or during a round collapse into one round over the union of the lists
// they named, and rounds are spaced further apart the longer they take; a round
// that never settles is released after a timeout.
//
// Frames also starve the lists' own polls: TanStack Query restarts a query's
// refetchInterval on every cache write, and each frame writes the list caches.
// So while frames keep arriving, a round over every list still runs every
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

/** What one round refetches: every list, or the lists with these query hashes. */
export type ReconcileScope = "all" | ReadonlySet<string>;

function mergeScopes(a: ReconcileScope | null, b: ReconcileScope): ReconcileScope {
  if (a === null) return b;
  if (a === "all" || b === "all") return "all";
  return new Set([...a, ...b]);
}

export interface ListReconcileScheduler {
  /** Ask for a reconcile (every list by default); coalesced with any pending or running one. */
  schedule: (scope?: ReconcileScope) => void;
  /** Record a frame; asks for a full reconcile if none started in RECONCILE_WHILE_ACTIVE_MS. */
  noteActivity: () => void;
  /** Drop a pending reconcile and stop scheduling new ones. */
  dispose: () => void;
}

/**
 * @param run - Runs one reconcile round over the merged scope; resolves once its
 *   refetches settle.
 * @param now - Clock, injectable for tests.
 */
export function createListReconcileScheduler(
  run: (scope: ReconcileScope) => Promise<unknown>,
  now: () => number = Date.now,
): ListReconcileScheduler {
  let timer: ReturnType<typeof setTimeout> | null = null;
  let running = false;
  // Lists asked for since the last round started; null when none were.
  let requested: ReconcileScope | null = null;
  let disposed = false;
  let lastEnd = Number.NEGATIVE_INFINITY;
  let lastDuration = 0;
  // The lists were just loaded when the stream starts, so that counts as a round
  // over every list. Rounds over named lists don't refresh the others.
  let lastFullStart = now();

  const fire = async (): Promise<void> => {
    const scope = requested ?? "all";
    requested = null;
    running = true;
    const started = now();
    if (scope === "all") lastFullStart = started;
    let roundTimer: ReturnType<typeof setTimeout> | undefined;
    try {
      await Promise.race([
        run(scope),
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
      if (requested !== null && !disposed) arm();
    }
  };

  const arm = (): void => {
    if (running || timer !== null) return;
    // A server that took long to answer gets proportionally fewer rounds.
    const gap = Math.min(RECONCILE_MAX_GAP_MS, Math.max(RECONCILE_MIN_GAP_MS, 2 * lastDuration));
    const delay = Math.max(RECONCILE_DEBOUNCE_MS, lastEnd + gap - now());
    timer = setTimeout(() => {
      timer = null;
      void fire();
    }, delay);
  };

  const schedule = (scope: ReconcileScope = "all"): void => {
    if (disposed || (scope !== "all" && scope.size === 0)) return;
    requested = mergeScopes(requested, scope);
    arm();
  };

  return {
    schedule,
    noteActivity() {
      if (now() - lastFullStart >= RECONCILE_WHILE_ACTIVE_MS) schedule();
    },
    dispose() {
      disposed = true;
      requested = null;
      if (timer !== null) clearTimeout(timer);
      timer = null;
    },
  };
}
