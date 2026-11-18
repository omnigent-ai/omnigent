// Reactive "is this a mobile-width viewport?" hook.
//
// The shell's responsive layout pivots on Tailwind's `md` breakpoint
// (`--breakpoint-md` in index.css), used both as CSS classes (`md:` / `max-md:`)
// and as the JS threshold in AppShell's `initialSidebarOpen`. This hook exposes
// the `max-md` side of that line to component logic that can't be expressed in
// CSS alone (e.g. swapping a hover flyout for an in-place page on touch).

import { useSyncExternalStore } from "react";

/** Tailwind's `md` breakpoint: an open foldable in landscape (951px) still gets the phone layout. */
export const MOBILE_BREAKPOINT_PX = 952;
/** Matches viewports at or above `md`, the desktop layout. */
export const DESKTOP_VIEWPORT_QUERY = `(min-width: ${MOBILE_BREAKPOINT_PX}px)`;

// Mirror Tailwind's `max-md` variant exactly so this hook stays in lockstep
// with the `max-md:` / `md:` classes already used across the shell.
const MOBILE_QUERY = `(max-width: ${MOBILE_BREAKPOINT_PX - 0.02}px)`;

function subscribe(callback: () => void): () => void {
  if (typeof window === "undefined" || !window.matchMedia) return () => {};
  const mql = window.matchMedia(MOBILE_QUERY);
  mql.addEventListener("change", callback);
  return () => mql.removeEventListener("change", callback);
}

function getSnapshot(): boolean {
  if (typeof window === "undefined" || !window.matchMedia) return false;
  return window.matchMedia(MOBILE_QUERY).matches;
}

/**
 * True when the viewport is narrower than Tailwind's `md` breakpoint (952px)
 * — i.e. the "mobile" layout the shell's `max-md:` classes target. Reactive:
 * components re-render when the viewport crosses the breakpoint. SSR-safe
 * (returns `false` on the server, matching `initialSidebarOpen`).
 */
export function useIsMobileViewport(): boolean {
  return useSyncExternalStore(subscribe, getSnapshot, () => false);
}
