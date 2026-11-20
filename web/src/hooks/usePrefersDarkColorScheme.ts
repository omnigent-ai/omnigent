// Reactive `(prefers-color-scheme: dark)`. In the desktop shell this follows
// the native theme source, so it settles after the shell applies a new source.

import { useSyncExternalStore } from "react";

const DARK_QUERY = "(prefers-color-scheme: dark)";

function subscribe(callback: () => void): () => void {
  if (typeof window === "undefined" || !window.matchMedia) return () => {};
  const mql = window.matchMedia(DARK_QUERY);
  mql.addEventListener("change", callback);
  return () => mql.removeEventListener("change", callback);
}

function getSnapshot(): boolean {
  if (typeof window === "undefined" || !window.matchMedia) return false;
  return window.matchMedia(DARK_QUERY).matches;
}

export function usePrefersDarkColorScheme(): boolean {
  return useSyncExternalStore(subscribe, getSnapshot, () => false);
}
