// Whether this client is driven by touch: the iOS or Android app, or a browser whose main
// pointer is a finger, as on a phone or tablet. Width alone doesn't say: a desktop window
// can be narrow, and a tablet wide.

import { useSyncExternalStore } from "react";

import { isAndroidShell, isIOSShell } from "@/lib/nativeBridge";

const COARSE_POINTER_QUERY = "(pointer: coarse)";

function subscribe(callback: () => void): () => void {
  if (typeof window === "undefined" || !window.matchMedia) return () => {};
  const query = window.matchMedia(COARSE_POINTER_QUERY);
  query.addEventListener("change", callback);
  return () => query.removeEventListener("change", callback);
}

/** True in the native mobile apps, or where the primary pointer is coarse. */
export function isTouchClient(): boolean {
  if (isIOSShell() || isAndroidShell()) return true;
  if (typeof window === "undefined" || !window.matchMedia) return false;
  return window.matchMedia(COARSE_POINTER_QUERY).matches;
}

/** {@link isTouchClient}, re-rendering when the primary pointer changes. */
export function useTouchClient(): boolean {
  return useSyncExternalStore(subscribe, isTouchClient, () => false);
}
