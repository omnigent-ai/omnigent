// Persisted, app-global preference for the default base branch to pre-fill
// when the user names a new worktree branch on the landing composer.
//
// Mirrors hostPreferences: the composer reads this each time the worktree
// dropdown opens to seed the base-branch field. When a default is stored, the
// field pre-fills so a new worktree branches off it; when nothing is stored,
// the field stays blank and the worktree defaults to the current branch. Set
// from the Git settings section.

const STORAGE_KEY = "omnigent:default-base-branch";

/**
 * Read the user's default base branch: the stored branch name, or `null` when
 * nothing is stored, on a server render (no `window`), or when storage is
 * inaccessible — never throws. Trims on read and treats a blank/whitespace-only
 * value as unset, so a hand-edited or stale entry can't display un-normalized.
 */
export function readDefaultBaseBranch(): string | null {
  if (typeof window === "undefined") return null;
  try {
    const stored = window.localStorage.getItem(STORAGE_KEY);
    const trimmed = stored?.trim() ?? "";
    return trimmed === "" ? null : trimmed;
  } catch {
    return null;
  }
}

/**
 * Persist `branch` as the user's default base branch. An empty (or
 * whitespace-only) value clears the preference, so auto-fill turns off.
 * Swallows quota/access errors so a failed write can't break settings.
 */
export function writeDefaultBaseBranch(branch: string): void {
  if (typeof window === "undefined") return;
  try {
    const trimmed = branch.trim();
    if (trimmed === "") {
      window.localStorage.removeItem(STORAGE_KEY);
    } else {
      window.localStorage.setItem(STORAGE_KEY, trimmed);
    }
  } catch {
    // localStorage quota or access errors shouldn't break settings.
  }
}
