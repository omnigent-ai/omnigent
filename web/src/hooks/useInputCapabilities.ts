// Single source of truth for pointer/input capability.
//
// Capability gates AFFORDANCES and defaults only (hit-target sizing,
// persistent vs hover-revealed controls, swipe hints) — never per-event
// handling. Gesture recognition must instead branch on the active sequence's
// `PointerEvent.pointerType`, so a touch on a fine-primary laptop still gets
// gesture semantics regardless of what these queries report. Viewport width
// is an independent LAYOUT axis — see `@/lib/breakpoints`.

import { useSyncExternalStore } from "react";

export interface InputCapabilities {
  /** ANY attached pointer is coarse — `(any-pointer: coarse)`. */
  anyCoarse: boolean;
  /** A touch digitizer is present. */
  hasTouch: boolean;
}

const CAPABILITY_QUERIES = ["(any-pointer: coarse)"] as const;

// SSR / no-matchMedia fallback: assume a hovering fine pointer (mouse
// desktop), matching the shell's historical desktop-first defaults.
const SERVER_SNAPSHOT: InputCapabilities = {
  anyCoarse: false,
  hasTouch: false,
};

let mediaLists: MediaQueryList[] | null = null;
const subscribers = new Set<() => void>();

function lists(): MediaQueryList[] {
  return (mediaLists ??= CAPABILITY_QUERIES.map((query) => window.matchMedia(query)));
}

function read(): InputCapabilities {
  if (typeof window === "undefined" || !window.matchMedia) return SERVER_SNAPSHOT;
  return {
    anyCoarse: lists()[0].matches,
    hasTouch: navigator.maxTouchPoints > 0,
  };
}

// useSyncExternalStore requires a referentially stable snapshot while values
// are unchanged; re-reading is cheap, so compare and keep the old object.
let cached: InputCapabilities = SERVER_SNAPSHOT;

function getSnapshot(): InputCapabilities {
  const next = read();
  if (next.anyCoarse !== cached.anyCoarse || next.hasTouch !== cached.hasTouch) {
    cached = next;
  }
  return cached;
}

function subscribe(callback: () => void): () => void {
  if (typeof window === "undefined" || !window.matchMedia) return () => {};
  if (subscribers.size === 0) {
    for (const list of lists()) list.addEventListener("change", emit);
  }
  subscribers.add(callback);
  return () => {
    subscribers.delete(callback);
    if (subscribers.size !== 0 || !mediaLists) return;
    for (const list of mediaLists) list.removeEventListener("change", emit);
    mediaLists = null;
  };
}

function emit() {
  for (const subscriber of subscribers) subscriber();
}

/**
 * Reactive coarse-pointer snapshot. Updates live when a touch-capable pointer
 * attaches or detaches. SSR-safe: the server snapshot assumes a mouse desktop.
 */
export function useInputCapabilities(): InputCapabilities {
  return useSyncExternalStore(subscribe, getSnapshot, () => SERVER_SNAPSHOT);
}
