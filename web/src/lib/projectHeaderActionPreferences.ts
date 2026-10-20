import { useSyncExternalStore } from "react";

export const SHOW_PROJECT_HEADER_ACTIONS_ON_TOUCH_STORAGE_KEY =
  "omnigent:show-project-header-actions-on-touch";
export const DEFAULT_SHOW_PROJECT_HEADER_ACTIONS_ON_TOUCH = false;

const PREFERENCE_CHANGED_EVENT = "omnigent:project-header-actions-on-touch-changed";
let snapshot = readShowProjectHeaderActionsOnTouch();
const listeners = new Set<() => void>();

/** Missing, malformed, or inaccessible storage keeps touch actions off. */
export function readShowProjectHeaderActionsOnTouch(): boolean {
  if (typeof window === "undefined") return DEFAULT_SHOW_PROJECT_HEADER_ACTIONS_ON_TOUCH;
  try {
    return window.localStorage.getItem(SHOW_PROJECT_HEADER_ACTIONS_ON_TOUCH_STORAGE_KEY) === "true";
  } catch {
    return DEFAULT_SHOW_PROJECT_HEADER_ACTIONS_ON_TOUCH;
  }
}

/** Save this device's touch project-header preference and refresh mounted views. */
export function writeShowProjectHeaderActionsOnTouch(value: boolean): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(SHOW_PROJECT_HEADER_ACTIONS_ON_TOUCH_STORAGE_KEY, String(value));
  } catch {
    // Storage access errors shouldn't break the app.
  }
  window.dispatchEvent(new Event(PREFERENCE_CHANGED_EVENT));
}

function refreshSnapshot(): void {
  snapshot = readShowProjectHeaderActionsOnTouch();
}

function getSnapshot(): boolean {
  return snapshot;
}

function handleChange(event: Event): void {
  if (
    event instanceof StorageEvent &&
    event.key !== null &&
    event.key !== SHOW_PROJECT_HEADER_ACTIONS_ON_TOUCH_STORAGE_KEY
  ) {
    return;
  }
  refreshSnapshot();
  for (const listener of listeners) listener();
}

function subscribe(onChange: () => void): () => void {
  if (typeof window === "undefined") return () => {};
  if (listeners.size === 0) {
    window.addEventListener("storage", handleChange);
    window.addEventListener(PREFERENCE_CHANGED_EVENT, handleChange);
    refreshSnapshot();
  }
  listeners.add(onChange);
  return () => {
    listeners.delete(onChange);
    if (listeners.size === 0) {
      window.removeEventListener("storage", handleChange);
      window.removeEventListener(PREFERENCE_CHANGED_EVENT, handleChange);
    }
  };
}

/** Live same-tab and cross-tab subscription; server rendering starts off. */
export function useShowProjectHeaderActionsOnTouch(): boolean {
  return useSyncExternalStore(
    subscribe,
    getSnapshot,
    () => DEFAULT_SHOW_PROJECT_HEADER_ACTIONS_ON_TOUCH,
  );
}
