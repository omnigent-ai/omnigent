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
 * Normalize an arbitrary parsed value to a valid preferences object, filling
 * each missing/unknown direction from {@link DEFAULT_SWIPE_ACTIONS}. Used both
 * on read (localStorage drift / manual edits) and to sanitize writes.
 */
export function normalizeSwipeActions(value: unknown): SwipeActionPreferences {
  const obj = typeof value === "object" && value !== null ? (value as Record<string, unknown>) : {};
  return {
    left: isSwipeAction(obj.left) ? obj.left : DEFAULT_SWIPE_ACTIONS.left,
    right: isSwipeAction(obj.right) ? obj.right : DEFAULT_SWIPE_ACTIONS.right,
  };
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

// Cached snapshot so useSyncExternalStore's getSnapshot returns a stable
// reference between changes — a fresh object every call would loop it. Kept
// current by getSnapshot (re-reads and swaps only on a real change), so it's
// never stale even after a write that had no active subscriber.
let snapshot: SwipeActionPreferences = readSwipeActions();

function getSnapshot(): SwipeActionPreferences {
  const next = readSwipeActions();
  if (next.left !== snapshot.left || next.right !== snapshot.right) snapshot = next;
  return snapshot;
}

function subscribe(onChange: () => void): () => void {
  if (typeof window === "undefined") return () => {};
  const handler = (e: Event) => {
    // Ignore unrelated `storage` events; refresh on our key or the same-tab ping.
    if (e instanceof StorageEvent && e.key !== null && e.key !== STORAGE_KEY) return;
    onChange();
  };
  window.addEventListener("storage", handler);
  window.addEventListener(SWIPE_ACTIONS_EVENT, handler);
  return () => {
    window.removeEventListener("storage", handler);
    window.removeEventListener(SWIPE_ACTIONS_EVENT, handler);
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
