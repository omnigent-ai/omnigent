import { QueryClient } from "@tanstack/react-query";
import { getSessionSlim } from "./sessionsApi";
import { sessionRoutingHostId, setSessionHost, setSessionParent } from "./sessionHost";
import type { Session } from "./types";

const browserHostQueries = new QueryClient();

/** Resolve the nearest own host binding; unlike cache-only getSessionHost in
 * sessionHost.ts, this fetches missing ancestors and can revalidate each hop. */
export async function fetchSessionHostId(
  sessionId: string,
  {
    fresh = false,
    queryClient = browserHostQueries,
  }: { fresh?: boolean; queryClient?: QueryClient } = {},
): Promise<string | null> {
  try {
    const visited = new Set<string>();
    let id: string | null = sessionId;
    while (id !== null && !visited.has(id)) {
      visited.add(id);
      const hopId: string = id;
      // oxlint-disable-next-line no-await-in-loop -- Each parent comes from the previous snapshot.
      const source: Session = await queryClient.fetchQuery({
        queryKey: ["session", hopId],
        queryFn: () => getSessionSlim(hopId),
        staleTime: fresh ? 0 : Infinity,
        retry: false,
      });
      setSessionHost(source.id, sessionRoutingHostId(source.hostId, source.labels));
      setSessionParent(source.id, source.parentSessionId);
      if (source.hostId) return source.hostId;
      id = source.parentSessionId;
    }
  } catch {
    // Unknown provenance stays public-only.
  }
  return null;
}
