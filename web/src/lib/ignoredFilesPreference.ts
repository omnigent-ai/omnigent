// Whether the Files tree and file searches show git-ignored files, such as
// node_modules or build output. A per-device preference that defaults to
// hiding them; it applies wherever this app reads a session's files itself
// (sessions on this Mac in the desktop app). Elsewhere files come from the
// session's runner, which lists everything.

import { useSyncExternalStore } from "react";

const STORAGE_KEY = "omnigent.files.showIgnored";
const CHANGE_EVENT = "omnigent:show-ignored-files-change";

/** Shown wherever the choice couldn't be stored. */
export const IGNORED_FILES_SAVE_FAILED_MESSAGE =
  "Couldn't save this preference in this browser or app. Try again, or check that site storage is allowed.";

/** Whether the user chose to see git-ignored files. Unreadable storage means no. */
export function readShowIgnoredFiles(): boolean {
  try {
    return window.localStorage.getItem(STORAGE_KEY) === "true";
  } catch {
    return false;
  }
}

/**
 * Store the choice and tell this window's listeners; other windows hear it
 * through the storage event.
 *
 * :returns: Whether the choice was stored.
 */
export function writeShowIgnoredFiles(show: boolean): boolean {
  try {
    if (show) window.localStorage.setItem(STORAGE_KEY, "true");
    else window.localStorage.removeItem(STORAGE_KEY);
  } catch {
    return false;
  }
  window.dispatchEvent(new Event(CHANGE_EVENT));
  return true;
}

/** Calls `listener` whenever the choice changes, in this window or another. */
export function subscribeShowIgnoredFiles(listener: () => void): () => void {
  const onStorage = (event: StorageEvent) => {
    if (event.key === null || event.key === STORAGE_KEY) listener();
  };
  window.addEventListener(CHANGE_EVENT, listener);
  window.addEventListener("storage", onStorage);
  return () => {
    window.removeEventListener(CHANGE_EVENT, listener);
    window.removeEventListener("storage", onStorage);
  };
}

/** The current choice, kept live across windows. */
export function useShowIgnoredFiles(): boolean {
  return useSyncExternalStore(subscribeShowIgnoredFiles, readShowIgnoredFiles, () => false);
}

/**
 * The choice as a query key segment, so changing it refetches the tree and
 * searches instead of showing answers made under the other choice.
 */
export function useIgnoredFilesQueryKey(): string {
  return ignoredFilesQueryKey(useShowIgnoredFiles());
}

/** The query key segment for a choice; the current one when none is given. */
export function ignoredFilesQueryKey(show: boolean = readShowIgnoredFiles()): string {
  return show ? "ignored:shown" : "ignored:hidden";
}
