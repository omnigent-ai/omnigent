/**
 * Remembering which host is the user's Arca instance (Databricks-internal).
 *
 * The only reliable signal is the host id captured when "Run on Arca"
 * connected it: a host row's `name` is the machine's hostname, which has no
 * dependable relationship to the arca instance name, so no heuristic
 * matching is attempted.
 */

const STORAGE_KEY = "omnigent:arca-host-id";

/** The host id last connected via Run on Arca, or null. */
export function readArcaHostId(): string | null {
  try {
    return localStorage.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
}

/** Remember (or with null, forget) the Arca host id. */
export function writeArcaHostId(hostId: string | null): void {
  try {
    if (hostId === null) localStorage.removeItem(STORAGE_KEY);
    else localStorage.setItem(STORAGE_KEY, hostId);
  } catch {
    // Storage unavailable (private mode) — the Arca option simply stays
    // offered; reconnecting an already-connected host is harmless.
  }
}
