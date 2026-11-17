// Per-device choice to soft-wrap long lines in chat code blocks and tool-call
// code panels. One toggle drives every block in every session and survives a
// refresh; the file editor's "Wrap lines" setting is separate.

import { useSyncExternalStore } from "react";
import { createStoredChoicePreference } from "./storedChoicePreference";

export const CODE_BLOCK_WORD_WRAP_STORAGE_KEY = "omnigent:code-block-word-wrap";

export const DEFAULT_CODE_BLOCK_WORD_WRAP = true;

const preference = createStoredChoicePreference(
  CODE_BLOCK_WORD_WRAP_STORAGE_KEY,
  ["false", "true"],
  DEFAULT_CODE_BLOCK_WORD_WRAP ? "true" : "false",
);

// This tab's choice while storage refuses to save it, so the toggle still works.
let unsavedWrap: boolean | undefined;

/** Read the preference; unknown values, and unreadable storage with no unsaved choice, mean the default. */
export function readCodeBlockWordWrap(): boolean {
  return unsavedWrap ?? preference.read() === "true";
}

/** Persist the preference and re-render every code block on screen. */
export function writeCodeBlockWordWrap(wrap: boolean): void {
  unsavedWrap = wrap;
  if (preference.write(wrap ? "true" : "false")) unsavedWrap = undefined;
}

/** Flip the preference from its current stored value. */
export function toggleCodeBlockWordWrap(): void {
  writeCodeBlockWordWrap(!readCodeBlockWordWrap());
}

/** Re-read after a same-tab write that bypassed `writeCodeBlockWordWrap` (settings import). */
export function notifyCodeBlockWordWrapChanged(): void {
  unsavedWrap = undefined;
  preference.notifyChanged();
}

// Another window's write replaces this tab's unsaved choice, even with no block mounted.
// Registered at load, so it runs before any reader's listener re-reads.
if (typeof window !== "undefined") {
  window.addEventListener("storage", (event) => {
    if (event.key !== null && event.key !== CODE_BLOCK_WORD_WRAP_STORAGE_KEY) return;
    try {
      if (event.storageArea !== null && event.storageArea !== window.localStorage) return;
    } catch {
      // Unreadable storage: readers re-read too, as in the preference's own listener.
    }
    unsavedWrap = undefined;
  });
}

/** Live value of the preference, including changes made in other windows. */
export function useCodeBlockWordWrap(): boolean {
  return useSyncExternalStore(
    preference.subscribe,
    readCodeBlockWordWrap,
    () => DEFAULT_CODE_BLOCK_WORD_WRAP,
  );
}
