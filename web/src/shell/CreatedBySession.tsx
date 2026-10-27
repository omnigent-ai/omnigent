import { useQuery } from "@tanstack/react-query";
import { Link } from "@/lib/routing";
import { getSessionSlim } from "@/lib/sessionsApi";
import { UNTITLED_CONVERSATION_LABEL } from "./sidebarNav";

export const CREATED_BY_SESSION_LABEL_KEY = "omnigent.created_by_session_id";

/** Resolve the creator through its own access-checked snapshot, never the worker's title. */
export function CreatedBySession({ sessionId }: { sessionId: string }) {
  // Previously cached session metadata does not establish current access.
  const {
    data: session,
    isFetchedAfterMount,
    isFetching,
    error,
  } = useQuery({
    queryKey: ["session-creator", sessionId],
    queryFn: () => getSessionSlim(sessionId),
    staleTime: 0,
    refetchOnMount: "always",
    retry: false,
  });
  if (!isFetchedAfterMount || isFetching) return null;
  return (
    <span className="min-w-0 truncate text-xs text-muted-foreground">
      Created by{" "}
      {session && !error ? (
        <Link
          to={`/c/${sessionId}`}
          className="hover:text-foreground hover:underline"
          title="Session that created this worker"
        >
          {session.title || UNTITLED_CONVERSATION_LABEL}
        </Link>
      ) : (
        "another session"
      )}
    </span>
  );
}
