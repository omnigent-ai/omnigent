// Per-device choice to list the files Git ignores in the Files tree, marked as
// ignored. Off by default, so the tree shows what the project tracks or could track.

import { createStoredChoicePreference } from "./storedChoicePreference";

const STORAGE_KEY = "omnigent:files-tree-show-ignored";

export const DEFAULT_SHOW_IGNORED_FILES = false;

const preference = createStoredChoicePreference(
  STORAGE_KEY,
  ["false", "true"],
  DEFAULT_SHOW_IGNORED_FILES ? "true" : "false",
);

/** Persist the preference and update every Files tree on screen. */
export function writeShowIgnoredFiles(show: boolean): void {
  preference.write(show ? "true" : "false");
}

/** Live value of the preference, including changes from ⌘K or another window. */
export function useShowIgnoredFiles(): boolean {
  return preference.use() === "true";
}
