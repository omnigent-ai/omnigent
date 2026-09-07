// Shared, slowly-ticking wall clock for relative time labels ("Next run in 3
// hours"). A relative delta goes stale as real time passes, so any component
// that renders one needs a periodic re-render to keep it fresh — otherwise the
// label freezes at its first-render value until something else re-renders.
//
// One MODULE-LEVEL `setInterval` drives every subscriber (same pattern as
// `useWorkingLabelTick` / `useIsMobileViewport`): a single timer regardless of
// how many rows are on screen, rather than one interval per row. Subscribers
// read the shared `now` via `useSyncExternalStore`; `getSnapshot` returns the
// SAME Date reference between ticks so React doesn't loop, and the timer swaps
// in a fresh Date (and notifies) once per `TICK_MS`. The timer starts lazily on
// the first subscriber and is torn down when the last one leaves — no leaks.

import { useSyncExternalStore } from "react";

// How often the shared clock advances. Deliberately coarse: the next-run label
// is minute-granular, so a 30s cadence keeps it at most ~30s stale while
// costing one cheap timer wakeup — far cheaper than a 1s countdown for a value
// that rarely changes wording.
export const TICK_MS = 30_000;

let currentNow = new Date();
let intervalId: ReturnType<typeof setInterval> | null = null;
const listeners = new Set<() => void>();

function subscribe(callback: () => void): () => void {
  listeners.add(callback);
  // Lazily start the one shared timer on the first subscriber. Refresh `now`
  // right away so a freshly-mounted subscriber doesn't briefly show a value
  // left over from a previous mount (the timer only advanced it while mounted).
  if (intervalId === null) {
    currentNow = new Date();
    intervalId = setInterval(() => {
      currentNow = new Date();
      for (const listener of listeners) listener();
    }, TICK_MS);
  }
  return () => {
    listeners.delete(callback);
    // Tear the timer down once nothing is listening.
    if (listeners.size === 0 && intervalId !== null) {
      clearInterval(intervalId);
      intervalId = null;
    }
  };
}

function getSnapshot(): Date {
  return currentNow;
}

/**
 * The current wall-clock time, refreshed every {@link TICK_MS} so relative time
 * labels re-render and stay fresh. Feed it as the `now` argument to
 * `formatNextRunAt` (or any relative formatter). All subscribers share a single
 * timer and the same `now` instant. SSR-safe (returns the module's `now`).
 */
export function useNow(): Date {
  return useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
}
