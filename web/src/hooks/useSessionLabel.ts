import { useQuery, useQueryClient } from "@tanstack/react-query";

import { type ChildSessionInfo, childSessionsQueryKey } from "@/hooks/useChildSessions";
import { useOptionalLoadedConversations } from "@/hooks/useSidebarData";
import { getSessionSlim } from "@/lib/sessionsApi";
import { sessionAgentIdentity, type SessionAgentIdentity } from "@/lib/sessionAgentIdentity";
import { childPrimaryLabel } from "@/shell/subagentLabel";

/** A session's title, kind and known agent identity, or null while unreadable. */
export interface SessionLabel {
  title: string;
  isSubagent: boolean;
  agent: SessionAgentIdentity | null;
  /** Session labels, e.g. the project identity a persona session works for. */
  labels?: Record<string, string>;
}

/**
 * Title of session ``sessionId`` for a pill. Reads the sidebar's loaded rows,
 * then the subagents of ``currentSessionId``, then the shared
 * ``["session", id]`` cache, and fetches that session only when all three miss.
 * Agent metadata uses the same lookup; repeated senders share it.
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
  const session = snapshot.data;
  if (row)
    return {
      title: row.title || "Untitled session",
      isSubagent: !!row.parent_session_id,
      agent: sessionAgentIdentity({
        agentName: row.agent_name ?? session?.agentName,
        harness: row.harness ?? session?.harness,
        labels: row.labels,
      }),
      labels: row.labels ?? session?.labels,
    };
  if (child)
    return {
      title: childPrimaryLabel(child),
      isSubagent: true,
      agent: sessionAgentIdentity({ ...session, labels: child.labels }),
      labels: child.labels,
    };
  if (!session) return null;
  return {
    title: session.title || "Untitled session",
    isSubagent: !!session.parentSessionId,
    agent: sessionAgentIdentity(session),
    labels: session.labels,
  };
}
