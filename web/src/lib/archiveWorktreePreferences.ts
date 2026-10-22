// App-global preference (Settings › Git): remove a session's git worktree
// when the session is archived. Unset means the user hasn't chosen yet, so the
// first archive of a worktree session prompts (see ArchiveWorktreeDialog).

export const DELETE_WORKTREES_ON_ARCHIVE_STORAGE_KEY = "omnigent:delete-worktrees-on-archive";

/**
 * Read the "delete worktrees for archived sessions" preference. `null` when
 * the user has never chosen (or on a server render / inaccessible storage), so
 * callers can tell "off" from "not yet asked". Never throws.
 */
export function readDeleteWorktreesOnArchive(): boolean | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = window.localStorage.getItem(DELETE_WORKTREES_ON_ARCHIVE_STORAGE_KEY);
    if (raw === "true") return true;
    if (raw === "false") return false;
    return null;
  } catch {
    return null;
  }
}

/**
 * Persist an explicit choice. Both values are stored so an explicit "off"
 * suppresses the archive prompt. Swallows quota/access errors.
 */
export function writeDeleteWorktreesOnArchive(on: boolean): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(DELETE_WORKTREES_ON_ARCHIVE_STORAGE_KEY, on ? "true" : "false");
  } catch {
    // localStorage quota or access errors shouldn't break settings.
  }
}
