import { useQuery } from "@tanstack/react-query";
import { Link } from "@/lib/routing";
import { getSessionSlim } from "@/lib/sessionsApi";
import type { Session } from "@/lib/types";
import { UNTITLED_CONVERSATION_LABEL } from "./sidebarNav";

export const CREATED_BY_SESSION_LABEL_KEY = "omnigent.created_by_session_id";

/**
 * A session's creator, read through its own access-checked snapshot. `creator` is
 * null when the viewer can't read it, e.g. after deletion. Each surface passes its
 * own `cacheScope` so one refetching doesn't blank another.
 */
export function useSessionCreator(
  sessionId: string,
  cacheScope?: string,
): { settled: boolean; creator: Session | null } {
  // Previously cached session metadata does not establish current access.
  const { data, isFetchedAfterMount, isFetching, error } = useQuery({
    queryKey: cacheScope
      ? ["session-creator", sessionId, cacheScope]
      : ["session-creator", sessionId],
    queryFn: () => getSessionSlim(sessionId),
    staleTime: 0,
    refetchOnMount: "always",
    retry: false,
  });
  return { settled: isFetchedAfterMount && !isFetching, creator: data && !error ? data : null };
}

/** Resolve the creator through its own access-checked snapshot, never the worker's title. */
export function CreatedBySession({ sessionId }: { sessionId: string }) {
  const { settled, creator } = useSessionCreator(sessionId);
  if (!settled) return null;
  return (
    <span className="min-w-0 truncate text-xs text-muted-foreground">
      Created by{" "}
      {creator ? (
        <Link
          to={`/c/${sessionId}`}
          className="hover:text-foreground hover:underline"
          title="Session that created this worker"
        >
          {creator.title || UNTITLED_CONVERSATION_LABEL}
        </Link>
      ) : (
        "another session"
      )}
    </span>
  );
}
