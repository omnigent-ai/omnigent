// Persisted, app-global preference for which host the new-session landing
// composer starts on.
//
// Mirrors agentPreferences: the landing screen keeps its live React state as
// the source of truth; these helpers only seed that state on mount and
// snapshot it when the user explicitly picks a host (or the managed sandbox),
// so the next visit starts from the last choice instead of the auto-picked
// default — the sandbox in managed deployments, the first online host in OSS.
// The consumer still validates a stored host id against the live host list and
// falls back to the default when that host is gone or offline.

const STORAGE_KEY = "omnigent:last-host-choice";

// Stored in place of a host id when the user picked the managed-sandbox option,
// which has no host id of its own (the server provisions the host at create
// time). A reserved sentinel so it can never collide with a real host id.
export const SANDBOX_HOST_CHOICE = "__sandbox__";

/**
 * Read the user's last explicit host choice on the landing composer: a host
 * id, the {@link SANDBOX_HOST_CHOICE} sentinel, or `null` when nothing is
 * stored, on a server render (no `window`), or when storage is inaccessible —
 * never throws.
 */
export function readLastHostChoice(): string | null {
  if (typeof window === "undefined") return null;
  try {
    return window.localStorage.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
}

/**
 * Persist `choice` (a host id or {@link SANDBOX_HOST_CHOICE}) as the user's
 * last explicit host pick. Swallows quota/access errors so a failed write
 * can't break session creation.
 */
export function writeLastHostChoice(choice: string): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(STORAGE_KEY, choice);
  } catch {
    // localStorage quota or access errors shouldn't break the composer.
  }
}
