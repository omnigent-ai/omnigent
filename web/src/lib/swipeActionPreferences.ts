// Per-device swipe actions; Settings and mounted rows share live preferences.
// Defaults: right archives, left deletes. Both directions are configurable.

import { useSyncExternalStore } from "react";

const STORAGE_KEY = "omnigent:swipe-actions";
// DOM `storage` events fire only in other tabs; this signal reaches the writer.
const SWIPE_ACTIONS_EVENT = "omnigent:swipe-actions-changed";

export const swipeActions = ["archive", "delete", "none"] as const;
export type SwipeAction = (typeof swipeActions)[number];

export type SwipeDirection = "left" | "right";

export interface SwipeActionPreferences {
  left: SwipeAction;
  right: SwipeAction;
}

/** Default: swipe-left deletes; swipe-right archives. */
export const DEFAULT_SWIPE_ACTIONS: SwipeActionPreferences = {
  left: "delete",
  right: "archive",
};

/** Return whether a string is one of the selectable swipe actions. */
export function isSwipeAction(value: unknown): value is SwipeAction {
  return typeof value === "string" && (swipeActions as readonly string[]).includes(value);
}

/** Missing directions default; unknown present actions become inert. */
export function normalizeSwipeActions(value: unknown): SwipeActionPreferences {
  const obj = typeof value === "object" && value !== null ? (value as Record<string, unknown>) : {};
  // An unrecognized stored direction stays inert rather than silently arming
  // a default action, especially the destructive delete gesture.
  function actionFor(direction: SwipeDirection): SwipeAction {
    const action = obj[direction];
    if (isSwipeAction(action)) return action;
    return Object.hasOwn(obj, direction) ? "none" : DEFAULT_SWIPE_ACTIONS[direction];
  }
  return { left: actionFor("left"), right: actionFor("right") };
}

/** SSR-safe read; missing, corrupt, or inaccessible storage uses defaults. */
export function readSwipeActions(): SwipeActionPreferences {
  if (typeof window === "undefined") return normalizeSwipeActions(null);
  try {
    return normalizeSwipeActions(JSON.parse(window.localStorage.getItem(STORAGE_KEY) ?? "null"));
  } catch {
    return normalizeSwipeActions(null);
  }
}

/** Persist sanitized actions without propagating storage errors. */
export function writeSwipeActions(value: SwipeActionPreferences): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(normalizeSwipeActions(value)));
  } catch {
    // localStorage quota or access errors shouldn't break the app.
  }
  // Refresh same-tab subscribers (the `storage` event only fires in other tabs).
  window.dispatchEvent(new Event(SWIPE_ACTIONS_EVENT));
}

// Keep a stable snapshot for useSyncExternalStore; refresh on changes or when
// subscribing after a write made while no row was mounted.
let snapshot: SwipeActionPreferences = readSwipeActions();

function refreshSnapshot(): void {
  const next = readSwipeActions();
  if (next.left !== snapshot.left || next.right !== snapshot.right) snapshot = next;
}

function getSnapshot(): SwipeActionPreferences {
  return snapshot;
}

// One window listener pair serves all mounted rows.
const listeners = new Set<() => void>();

function handleChange(e: Event): void {
  // Ignore unrelated `storage` events; refresh on our key or the same-tab ping.
  if (e instanceof StorageEvent && e.key !== null && e.key !== STORAGE_KEY) return;
  refreshSnapshot();
  for (const listener of listeners) listener();
}

function subscribe(onChange: () => void): () => void {
  if (typeof window === "undefined") return () => {};
  if (listeners.size === 0) {
    window.addEventListener("storage", handleChange);
    window.addEventListener(SWIPE_ACTIONS_EVENT, handleChange);
    // Catch up on writes made with no subscribers mounted.
    refreshSnapshot();
  }
  listeners.add(onChange);
  return () => {
    listeners.delete(onChange);
    if (listeners.size === 0) {
      window.removeEventListener("storage", handleChange);
      window.removeEventListener(SWIPE_ACTIONS_EVENT, handleChange);
    }
  };
}

/** Live same-tab/cross-tab preference subscription; SSR renders defaults. */
export function useSwipeActions(): SwipeActionPreferences {
  return useSyncExternalStore(subscribe, getSnapshot, () => DEFAULT_SWIPE_ACTIONS);
}
