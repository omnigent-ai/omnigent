import { useQuery, useQueryClient } from "@tanstack/react-query";

import { type ChildSessionInfo, childSessionsQueryKey } from "@/hooks/useChildSessions";
import { useOptionalLoadedConversations } from "@/hooks/useSidebarData";
import { getSessionSlim } from "@/lib/sessionsApi";
import { childPrimaryLabel } from "@/shell/subagentLabel";

/** A session's display title and kind, or ``null`` while unknown or unreadable. */
export interface SessionLabel {
  title: string;
  isSubagent: boolean;
}

/**
 * Title of session ``sessionId`` for a pill. Reads the sidebar's loaded rows,
 * then the subagents of ``currentSessionId``, then the shared
 * ``["session", id]`` cache, and fetches that session only when all three miss.
 *
 * :param sessionId: Session to label, or ``null`` to skip.
 * :param currentSessionId: Session being viewed, whose subagents are checked.
 * :returns: The label, or ``null`` while loading or when the session is unreadable.
 */
export function useSessionLabel(
  sessionId: string | null,
  currentSessionId: string | null,
): SessionLabel | null {
  const queryClient = useQueryClient();
  const loaded = useOptionalLoadedConversations();
  const row = sessionId
    ? loaded?.pages.flatMap((page) => page.data).find((candidate) => candidate.id === sessionId)
    : undefined;
  const child =
    sessionId && currentSessionId && !row
      ? queryClient
          .getQueryData<ChildSessionInfo[]>(childSessionsQueryKey(currentSessionId))
          ?.find((candidate) => candidate.id === sessionId)
      : undefined;
  const cached = row !== undefined || child !== undefined;
  const snapshot = useQuery({
    queryKey: ["session", sessionId] as const,
    queryFn: () => getSessionSlim(sessionId as string),
    enabled: sessionId !== null && !cached,
    staleTime: Infinity,
    // An unreadable sender stays "another session" for the tab, not one lookup per bubble.
    retry: false,
    retryOnMount: false,
  });
  if (row) return { title: row.title || "Untitled session", isSubagent: !!row.parent_session_id };
  if (child) return { title: childPrimaryLabel(child), isSubagent: true };
  const session = snapshot.data;
  if (!session) return null;
  return { title: session.title || "Untitled session", isSubagent: !!session.parentSessionId };
}
