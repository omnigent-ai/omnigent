import { useQuery } from "@tanstack/react-query";
import { getSessionSlim } from "@/lib/sessionsApi";
import type { Session } from "@/lib/types";

export const CREATED_BY_SESSION_LABEL_KEY = "omnigent.created_by_session_id";

/**
 * A session's creator, read through its own access-checked snapshot. `creator` is
 * null when the viewer can't read it, e.g. after deletion.
 */
export function useSessionCreator(sessionId: string): {
  settled: boolean;
  creator: Session | null;
} {
  // Previously cached session metadata does not establish current access.
  const { data, isFetchedAfterMount, isFetching, error } = useQuery({
    queryKey: ["session-creator", sessionId],
    queryFn: () => getSessionSlim(sessionId),
    staleTime: 0,
    refetchOnMount: "always",
    retry: false,
  });
  return { settled: isFetchedAfterMount && !isFetching, creator: data && !error ? data : null };
}
