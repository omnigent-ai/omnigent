// Persisted, app-global preference for which agent the new-session
// landing composer starts on.
//
// The landing screen keeps its live React state as the source of truth;
// these helpers only seed that state on mount and snapshot it when the
// user explicitly picks an agent, so the next visit starts from the last
// choice instead of the catalog's first entry. Built-in agent ids are
// name-derived server-side and survive reseeds, so the id is the durable
// reference; the consumer still validates it against the live agent list
// and falls back to the default when the stored agent no longer exists.

const STORAGE_KEY = "omnigent:last-agent-id";

/**
 * Read the last agent id the user picked on the landing composer.
 * Returns `null` when nothing is stored, on a server render (no
 * `window`), or when storage is inaccessible — never throws.
 */
export function readLastAgentId(): string | null {
  if (typeof window === "undefined") return null;
  try {
    return window.localStorage.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
}

/**
 * Persist `agentId` as the user's last explicit agent pick. Swallows
 * quota/access errors so a failed write can't break session creation.
 */
export function writeLastAgentId(agentId: string): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(STORAGE_KEY, agentId);
  } catch {
    // localStorage quota or access errors shouldn't break the composer.
  }
}
