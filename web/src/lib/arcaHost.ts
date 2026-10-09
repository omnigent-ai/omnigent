/** Isaac seeds "<user>'s arca"; accept that suffix or the id saved by Run on Arca. */

import type { Host } from "@/hooks/useHosts";

export const ARCA_HOST_ID_STORAGE_KEY = "omnigent:arca-host-id";

export function isArcaHost(
  host: Pick<Host, "host_id" | "name">,
  storedArcaHostId: string | null,
): boolean {
  return (
    host.name.endsWith("'s arca") ||
    (storedArcaHostId !== null && host.host_id === storedArcaHostId)
  );
}

/** The host id last connected via Run on Arca, or null. */
export function readArcaHostId(): string | null {
  try {
    return localStorage.getItem(ARCA_HOST_ID_STORAGE_KEY);
  } catch {
    return null;
  }
}

/** Remember (or with null, forget) the Arca host id. */
export function writeArcaHostId(hostId: string | null): void {
  try {
    if (hostId === null) localStorage.removeItem(ARCA_HOST_ID_STORAGE_KEY);
    else localStorage.setItem(ARCA_HOST_ID_STORAGE_KEY, hostId);
  } catch {
    // Storage unavailable (private mode) — the Arca option simply stays
    // offered; reconnecting an already-connected host is harmless.
  }
}
