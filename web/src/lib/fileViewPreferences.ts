// Persisted, app-global preferences for how the file viewer renders files.
//
// These are *preferences*, not per-file or per-session state: the diff
// on/off toggle, the split/unified layout, and the source/preview mode all
// "carry over" as the user navigates between files (see FileViewer's
// "Diff is a global toggle" comment), and should also survive a page
// refresh. They're stored under a single localStorage key with no
// per-conversation keying, mirroring that global-toggle semantics.
//
// FileViewer keeps the live React state as the source of truth for the UI;
// these helpers only seed that state on mount and snapshot it on change, so
// a refresh (or a brand-new conversation) starts from the user's last
// choice instead of the hardcoded defaults.

import { useSyncExternalStore } from "react";

import { isStorageEventFor } from "./storedChoicePreference";

export interface FileViewPreferences {
  /** Whether the diff view is the preferred mode for changed files. */
  diffActive: boolean;
  /** How diff hunks render: inline ("unified") or side-by-side ("split"). */
  diffLayout: "unified" | "split";
  /** Preferred mode for previewable (markdown/html) files. */
  previewableViewMode: "editor" | "preview" | "source";
  /** Whether whitespace-only changes are hidden in the diff view. */
  hideWhitespace: boolean;
  /** Whether long lines soft-wrap in the code editor and diff view (no horizontal scroll). */
  wrapLines: boolean;
  /** Whether the code viewer pins the lines opening the scopes around the top of the view. */
  stickyScroll: boolean;
}

const STORAGE_KEY = "omnigent:file-view-preferences";
const CHANGE_EVENT = `${STORAGE_KEY}-change`;

export const DEFAULT_FILE_VIEW_PREFERENCES: FileViewPreferences = {
  diffActive: false,
  diffLayout: "unified",
  previewableViewMode: "editor",
  hideWhitespace: false,
  wrapLines: false,
  stickyScroll: true,
};

/**
 * Read the persisted file-view preferences. Returns the defaults when
 * nothing is stored, on a server render (no `window`), or when the stored
 * value is malformed — never throws, so a corrupt entry can't break the app.
 * Each field is validated independently so a partial/garbage record still
 * yields sane values for the fields that are valid.
 */
export function readFileViewPreferences(): FileViewPreferences {
  if (typeof window === "undefined") return DEFAULT_FILE_VIEW_PREFERENCES;
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return DEFAULT_FILE_VIEW_PREFERENCES;
    const parsed: unknown = JSON.parse(raw);
    if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
      return DEFAULT_FILE_VIEW_PREFERENCES;
    }
    const p = parsed as Record<string, unknown>;
    return {
      diffActive:
        typeof p.diffActive === "boolean" ? p.diffActive : DEFAULT_FILE_VIEW_PREFERENCES.diffActive,
      diffLayout: p.diffLayout === "split" ? "split" : "unified",
      previewableViewMode:
        p.previewableViewMode === "preview" || p.previewableViewMode === "source"
          ? p.previewableViewMode
          : "editor",
      hideWhitespace:
        typeof p.hideWhitespace === "boolean"
          ? p.hideWhitespace
          : DEFAULT_FILE_VIEW_PREFERENCES.hideWhitespace,
      wrapLines:
        typeof p.wrapLines === "boolean" ? p.wrapLines : DEFAULT_FILE_VIEW_PREFERENCES.wrapLines,
      stickyScroll:
        typeof p.stickyScroll === "boolean"
          ? p.stickyScroll
          : DEFAULT_FILE_VIEW_PREFERENCES.stickyScroll,
    };
  } catch {
    return DEFAULT_FILE_VIEW_PREFERENCES;
  }
}

/**
 * Persist the file-view preferences. Swallows quota/access errors so a
 * failed write can't break the viewer.
 */
export function writeFileViewPreferences(prefs: FileViewPreferences): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(prefs));
  } catch {
    // localStorage quota or access errors shouldn't break the app.
  }
  window.dispatchEvent(new Event(CHANGE_EVENT));
}

function subscribe(listener: () => void): () => void {
  const onStorage = (event: StorageEvent) => {
    if (isStorageEventFor(event, STORAGE_KEY)) listener();
  };
  window.addEventListener(CHANGE_EVENT, listener);
  window.addEventListener("storage", onStorage);
  return () => {
    window.removeEventListener(CHANGE_EVENT, listener);
    window.removeEventListener("storage", onStorage);
  };
}

/** Live sticky scroll choice, including changes from ⌘K or another window. */
export function useStickyScroll(): boolean {
  return useSyncExternalStore(
    subscribe,
    () => readFileViewPreferences().stickyScroll,
    () => DEFAULT_FILE_VIEW_PREFERENCES.stickyScroll,
  );
}

/** Turn sticky scroll on or off, keeping the other file-view preferences. */
export function writeStickyScroll(enabled: boolean): void {
  writeFileViewPreferences({ ...readFileViewPreferences(), stickyScroll: enabled });
}
