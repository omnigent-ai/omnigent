// Persisted, per-device preference for showing wall-clock timestamps next to
// transcript messages (user prompts and assistant responses).
//
// On by default: session transcripts double as a record of when work
// happened, so the stamps carry signal out of the box. It's a device-local
// UI preference — no account or session state is changed — so it lives in
// localStorage like the other `*Preferences` helpers.
//
// Unlike most preference helpers, this one also exposes a subscription hook:
// message bubbles are memoized deep inside the transcript, so a Settings
// toggle must notify them directly rather than rely on a remount.

import { useSyncExternalStore } from "react";

const STORAGE_KEY = "omnigent:show-message-timestamps";

export const DEFAULT_SHOW_MESSAGE_TIMESTAMPS = true;

const listeners = new Set<() => void>();

/**
 * Read the persisted "show message timestamps" preference. Returns the
 * default (on) when nothing is stored, on a server render (no `window`), or
 * when the stored value is malformed — never throws, so a corrupt entry can't
 * break the app.
 */
export function readShowMessageTimestamps(): boolean {
  if (typeof window === "undefined") return DEFAULT_SHOW_MESSAGE_TIMESTAMPS;
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (raw === null) return DEFAULT_SHOW_MESSAGE_TIMESTAMPS;
    return raw !== "false";
  } catch {
    return DEFAULT_SHOW_MESSAGE_TIMESTAMPS;
  }
}

/**
 * Persist the "show message timestamps" preference and notify subscribed
 * components. Swallows quota/access errors so a failed write can't break
 * the app.
 */
export function writeShowMessageTimestamps(value: boolean): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(STORAGE_KEY, value ? "true" : "false");
  } catch {
    // localStorage quota or access errors shouldn't break the app.
  }
  for (const listener of listeners) listener();
}

function subscribe(callback: () => void): () => void {
  listeners.add(callback);
  return () => {
    listeners.delete(callback);
  };
}

function getSnapshot(): boolean {
  return readShowMessageTimestamps();
}

/**
 * The live "show message timestamps" preference. Re-renders the subscriber
 * when the Settings toggle flips, including bubbles already mounted behind
 * a memo boundary. SSR-safe (returns the default).
 */
export function useShowMessageTimestamps(): boolean {
  return useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
}
