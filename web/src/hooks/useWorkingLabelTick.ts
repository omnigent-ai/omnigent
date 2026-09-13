// Shared wall-clock tick for the rotating "Working…" label.
//
// The busy indicator renders in two places (the inline shimmer and the
// scroll-pinned pill). Both cycle the same label pool, so they derive their
// index from ONE module-level timer: a single `setInterval` shared via
// `useSyncExternalStore` (same pattern as `useIsMobileViewport`). Reading
// `Date.now()` in `getSnapshot` means every subscriber lands on the same
// bucket, so the two sites stay in lockstep with zero drift.
//
// Deliberately NOT gated on `prefers-reduced-motion`: a text swap isn't CSS
// motion, so the label keeps rotating while the shimmer sweep and Otto's bob
// freeze (that gate lives in index.css).

import { useSyncExternalStore } from "react";

// How long each label stays on screen before rotating. Snappy on purpose: the
// label visibly cycles within a single turn so the indicator reads as active
// motion ("still moving") rather than a frozen status. The bucket is wall-clock
// aligned, so every subscriber lands on the same label with zero drift.
export const ROTATE_MS = 5 * 1000; // 5 seconds

let intervalId: ReturnType<typeof setInterval> | null = null;
const listeners = new Set<() => void>();

function subscribe(callback: () => void): () => void {
  listeners.add(callback);
  // Lazily start the one shared timer on the first subscriber.
  if (intervalId === null) {
    intervalId = setInterval(() => {
      for (const listener of listeners) listener();
    }, ROTATE_MS);
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

function getSnapshot(): number {
  return Math.floor(Date.now() / ROTATE_MS);
}

/**
 * Monotonic wall-clock bucket that advances once every `ROTATE_MS` (5s). Feed
 * it into `workingIndicatorLabel(tick)` to rotate the label. SSR-safe
 * (returns 0 on the server, matching `useIsMobileViewport`).
 */
export function useWorkingLabelTick(): number {
  return useSyncExternalStore(subscribe, getSnapshot, () => 0);
}
