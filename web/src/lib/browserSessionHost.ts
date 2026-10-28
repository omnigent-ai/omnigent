import { getSessionSlim } from "./sessionsApi";
import { sessionRoutingHostId, setSessionHost, setSessionParent } from "./sessionHost";
import type { Session } from "./types";

/** Browser permissions use bound session hosts, never routing labels or parent hints. */
export async function resolveBrowserSessionHostId(sessionId: string): Promise<string | null> {
  const visited = new Set<string>();
  let id: string | null = sessionId;
  try {
    while (id !== null && !visited.has(id)) {
      visited.add(id);
      // oxlint-disable-next-line no-await-in-loop -- Each parent comes from the previous snapshot.
      const source: Session = await getSessionSlim(id);
      setSessionHost(source.id, sessionRoutingHostId(source.hostId, source.labels));
      setSessionParent(source.id, source.parentSessionId);
      if (source.hostId) return source.hostId;
      id = source.parentSessionId;
    }
  } catch {
    // Unknown provenance cannot authorize local browser access.
  }
  return null;
}
