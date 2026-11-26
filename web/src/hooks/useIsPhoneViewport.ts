// Reactive "is this a phone-width viewport?" hook: Tailwind's `max-sm` (below 640px).

import { useSyncExternalStore } from "react";

const PHONE_QUERY = "(max-width: 639.98px)";

function subscribe(callback: () => void): () => void {
  if (typeof window === "undefined" || !window.matchMedia) return () => {};
  const mql = window.matchMedia(PHONE_QUERY);
  mql.addEventListener("change", callback);
  return () => mql.removeEventListener("change", callback);
}

function getSnapshot(): boolean {
  if (typeof window === "undefined" || !window.matchMedia) return false;
  return window.matchMedia(PHONE_QUERY).matches;
}

/** True below Tailwind's `sm` breakpoint, where project pages stack into phone rows. */
export function useIsPhoneViewport(): boolean {
  return useSyncExternalStore(subscribe, getSnapshot, () => false);
}
