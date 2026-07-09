// Persisted, app-global preference for the default base branch to pre-fill
// when the user names a new worktree branch on the landing composer.
//
// Mirrors hostPreferences: the landing screen keeps its live React state as
// the source of truth; these helpers only seed that state on mount. When a
// default is stored, the composer pre-fills the base-branch field so a new
// worktree branches off it; when nothing is stored, the field stays blank and
// the worktree defaults to the current branch. Set from the Git settings
// section.

const STORAGE_KEY = "omnigent:default-base-branch";
// A same-tab write to localStorage does NOT fire the `storage` event (that only
// fires in OTHER tabs), so we announce same-tab changes on this custom event.
// The composer subscribes to both so it can live-follow the setting.
const CHANGE_EVENT = "omnigent:default-base-branch-changed";

/**
 * Read the user's default base branch: the stored branch name, or `null` when
 * nothing is stored, on a server render (no `window`), or when storage is
 * inaccessible — never throws.
 */
export function readDefaultBaseBranch(): string | null {
  if (typeof window === "undefined") return null;
  try {
    return window.localStorage.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
}

/**
 * Persist `branch` as the user's default base branch. An empty (or
 * whitespace-only) value clears the preference, so auto-fill turns off.
 * Swallows quota/access errors so a failed write can't break settings.
 * Announces the change on {@link CHANGE_EVENT} so a mounted composer in the
 * same tab picks it up without a refresh.
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
  try {
    window.dispatchEvent(new Event(CHANGE_EVENT));
  } catch {
    // A missing Event constructor (non-DOM env) must not break the write.
  }
}

/**
 * Subscribe to default-base-branch changes — same-tab writes (via
 * {@link writeDefaultBaseBranch}) and cross-tab `storage` events both invoke
 * `onChange`. Returns an unsubscribe function; a no-op on a server render.
 */
export function subscribeDefaultBaseBranch(onChange: () => void): () => void {
  if (typeof window === "undefined") return () => {};
  const sameTab = () => onChange();
  const crossTab = (e: StorageEvent) => {
    if (e.key === STORAGE_KEY) onChange();
  };
  window.addEventListener(CHANGE_EVENT, sameTab);
  window.addEventListener("storage", crossTab);
  return () => {
    window.removeEventListener(CHANGE_EVENT, sameTab);
    window.removeEventListener("storage", crossTab);
  };
}
