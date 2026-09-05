// App-global default (Settings › Git): start new git sessions in a fresh
// randomly-named worktree. A project's stored `use_worktree` overrides it;
// an unset project falls through to this. Mirrors baseBranchPreferences.

const STORAGE_KEY = "omnigent:always-use-worktree";

/**
 * Read the global "always use a worktree" default. `false` on a server render
 * (no `window`), when nothing is stored, or when storage is inaccessible —
 * never throws. Only the literal string "true" reads as on, so a stale/hand-
 * edited value can't accidentally enable it.
 */
export function readAlwaysUseWorktree(): boolean {
  if (typeof window === "undefined") return false;
  try {
    return window.localStorage.getItem(STORAGE_KEY) === "true";
  } catch {
    return false;
  }
}

/**
 * Persist the global "always use a worktree" default. Storing `false` clears
 * the key so the absence of the key is the off state. Swallows quota/access
 * errors so a failed write can't break settings.
 */
export function writeAlwaysUseWorktree(on: boolean): void {
  if (typeof window === "undefined") return;
  try {
    if (on) {
      window.localStorage.setItem(STORAGE_KEY, "true");
    } else {
      window.localStorage.removeItem(STORAGE_KEY);
    }
  } catch {
    // localStorage quota or access errors shouldn't break settings.
  }
}
