// Reactive "is the primary pointer coarse?" hook.
//
// `(pointer: coarse)` matches touch-primary devices (phones, tablets) and is
// the standard signal for "there is no practical Shift+Enter": the on-screen
// keyboard's Enter is the only newline key such devices have. Used to keep
// Enter an inserting key (rather than a submitting one) in text inputs where
// submission already has an explicit button.

import { useSyncExternalStore } from "react";

const COARSE_QUERY = "(pointer: coarse)";

function subscribe(callback: () => void): () => void {
  if (typeof window === "undefined" || !window.matchMedia) return () => {};
  const mql = window.matchMedia(COARSE_QUERY);
  mql.addEventListener("change", callback);
  return () => mql.removeEventListener("change", callback);
}

function getSnapshot(): boolean {
  if (typeof window === "undefined" || !window.matchMedia) return false;
  return window.matchMedia(COARSE_QUERY).matches;
}

/**
 * True when the device's primary pointer is coarse (touch). Reactive across
 * pointer changes, SSR-safe (returns `false` on the server).
 */
export function useIsCoarsePointer(): boolean {
  return useSyncExternalStore(subscribe, getSnapshot, () => false);
}
