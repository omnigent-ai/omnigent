// Reactive "does plain Enter insert a newline instead of sending?" hook.
//
// Chosen from the device rather than from the responsive layout. The mobile
// layout reaches 1180px so tablets get it, but a desktop browser window that
// narrow still has a hardware keyboard with Shift+Enter. Enter stays a newline
// where the on-screen keyboard is the only one: a touch-primary pointer, or a
// phone-width window in the mobile layout. The desktop app and the floating
// New Agent panel never use the mobile layout, so they keep Enter-to-send at
// every width.

import { useSyncExternalStore } from "react";

import { useIsMobileViewport } from "@/hooks/useIsMobileViewport";

/** Touch-primary devices (phones, tablets), whose on-screen Enter is the only newline key. */
export const COARSE_POINTER_QUERY = "(pointer: coarse)";
/** Phone widths: the mobile layout's breakpoint before it grew to cover tablets. */
export const PHONE_WIDTH_QUERY = "(max-width: 767.98px)";

function subscribe(callback: () => void): () => void {
  if (typeof window === "undefined" || !window.matchMedia) return () => {};
  const queries = [COARSE_POINTER_QUERY, PHONE_WIDTH_QUERY].map((query) =>
    window.matchMedia(query),
  );
  for (const mql of queries) mql.addEventListener("change", callback);
  return () => {
    for (const mql of queries) mql.removeEventListener("change", callback);
  };
}

function matches(query: string): boolean {
  if (typeof window === "undefined" || !window.matchMedia) return false;
  return window.matchMedia(query).matches;
}

function getSnapshot(): number {
  // One number, so React sees a change when either query flips.
  return (matches(COARSE_POINTER_QUERY) ? 1 : 0) | (matches(PHONE_WIDTH_QUERY) ? 2 : 0);
}

/**
 * True when plain Enter should insert a newline and sending needs the Send
 * button: on a touch-primary device, or in a phone-width mobile layout. False
 * in a desktop browser window of any other width, so the
 * "Submit with ⌘ + Enter" preference decides. Reactive, SSR-safe (false on the
 * server).
 */
export function usePreventsKeyboardSubmit(): boolean {
  const mobileLayout = useIsMobileViewport();
  const state = useSyncExternalStore(subscribe, getSnapshot, () => 0);
  const coarsePointer = (state & 1) !== 0;
  const phoneWidth = (state & 2) !== 0;
  return coarsePointer || (mobileLayout && phoneWidth);
}
