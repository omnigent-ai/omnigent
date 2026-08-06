// Persisted, per-device preference for what a horizontal swipe on a session
// row does on touch devices. Two independent directions — swipe-left and
// swipe-right — each map to an action. Modeled after the other `*Preferences`
// helpers: a localStorage-backed value, SSR-safe reads, and writes that swallow
// quota/access errors so a corrupt entry can't break app boot.
//
// Unlike the simpler helpers this one also exposes a live subscription
// (useSwipeActions): Settings and every mounted session row read one source, so
// changing the preference updates open rows in the same session without a
// reload. writeSwipeActions notifies same-tab subscribers; a `storage` listener
// covers other tabs.
//
// Defaults mirror a phone mail app: swipe-left archives (the common tidy-away
// gesture), swipe-right is inert. Both directions may map to the SAME action —
// nothing prevents e.g. archiving on either side.

import { useSyncExternalStore } from "react";

const STORAGE_KEY = "omnigent:swipe-actions";
// Same-tab change signal. The DOM `storage` event only fires in OTHER tabs, so
// writeSwipeActions dispatches this to refresh subscribers in the writing tab.
const SWIPE_ACTIONS_EVENT = "omnigent:swipe-actions-changed";

export const swipeActions = ["archive", "delete", "none"] as const;
export type SwipeAction = (typeof swipeActions)[number];

export type SwipeDirection = "left" | "right";

export interface SwipeActionPreferences {
  left: SwipeAction;
  right: SwipeAction;
}

/** Default: swipe-left archives, swipe-right does nothing. */
export const DEFAULT_SWIPE_ACTIONS: SwipeActionPreferences = {
  left: "archive",
  right: "none",
};

/** Return whether a string is one of the selectable swipe actions. */
export function isSwipeAction(value: unknown): value is SwipeAction {
  return value === "archive" || value === "delete" || value === "none";
}

/**
 * Normalize an arbitrary parsed value to a valid preferences object. Missing
 * directions use the defaults; present but unknown actions become inert.
 * Used both on read (localStorage drift / manual edits) and to sanitize writes.
 */
export function normalizeSwipeActions(value: unknown): SwipeActionPreferences {
  const obj = typeof value === "object" && value !== null ? (value as Record<string, unknown>) : {};
  // A direction set to something unrecognized goes inert rather than inheriting
  // the default, which would silently arm archive on a swipe meant to be safe.
  function actionFor(direction: SwipeDirection): SwipeAction {
    const action = obj[direction];
    if (isSwipeAction(action)) return action;
    return Object.hasOwn(obj, direction) ? "none" : DEFAULT_SWIPE_ACTIONS[direction];
  }
  return { left: actionFor("left"), right: actionFor("right") };
}

/**
 * Read the persisted swipe actions. Returns the defaults when nothing is
 * stored, on a server render (no `window`), or when the stored value is
 * missing/malformed — never throws, so a corrupt entry can't break app boot.
 */
export function readSwipeActions(): SwipeActionPreferences {
  if (typeof window === "undefined") return { ...DEFAULT_SWIPE_ACTIONS };
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return { ...DEFAULT_SWIPE_ACTIONS };
    return normalizeSwipeActions(JSON.parse(raw));
  } catch {
    return { ...DEFAULT_SWIPE_ACTIONS };
  }
}

/**
 * Persist the swipe actions. Normalizes first so only valid values land in
 * storage. Swallows quota/access errors so a failed write can't break the app.
 */
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

// Cached snapshot so useSyncExternalStore's getSnapshot is a cheap identity
// read on every render — a fresh object every call would loop it, and a
// re-parse every call would tax every row render. Refreshed on the change
// events and when a subscriber attaches (covering writes that happened while
// nothing was mounted); swapped only on a real change so the reference is
// stable between changes.
let snapshot: SwipeActionPreferences = readSwipeActions();

function refreshSnapshot(): void {
  const next = readSwipeActions();
  if (next.left !== snapshot.left || next.right !== snapshot.right) snapshot = next;
}

function getSnapshot(): SwipeActionPreferences {
  return snapshot;
}

// One module-level window listener pair fanning out to a shared set (the
// useMediaQuery shape): N mounted rows cost one registration, not 2N.
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
    // Catch up on writes made while no subscriber was mounted (React re-checks
    // the snapshot right after subscribing, so a change here still renders).
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

/**
 * Subscribe to the live swipe-action preference. Returns the current value and
 * re-renders on same-tab writes (via writeSwipeActions) and cross-tab `storage`
 * events, so Settings and every mounted session row share one source of truth.
 * SSR-safe: renders the defaults on the server.
 */
export function useSwipeActions(): SwipeActionPreferences {
  return useSyncExternalStore(subscribe, getSnapshot, () => DEFAULT_SWIPE_ACTIONS);
}
