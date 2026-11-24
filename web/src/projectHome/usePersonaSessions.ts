import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo } from "react";
import { fetchConversationById } from "@/hooks/useOpenSessionRow";
import { fetchSharedProjectSessions, type Conversation } from "@/hooks/useConversations";
import { listProjects } from "@/lib/projectsApi";
import { isSharedProjectPersona } from "@/shell/sidebarNav";
import { findCachedListRow } from "@/lib/sessionListCache";
import { PROJECT_IDENTITY_LABEL_KEY } from "@/lib/personaAvatar";
import { useProjectIdentities } from "./useProjectIdentities";

/**
 * Each project's Lead session id. Keyed under `["projects"]` so the
 * project-change events that refresh the sidebar refresh this too.
 */
export function useOrchestratorSessionIds(): ReadonlySet<string> {
  const query = useQuery({
    queryKey: ["projects", "main-sessions"],
    queryFn: listProjects,
    staleTime: 30_000,
  });
  return useMemo(
    () =>
      new Set(
        (query.data ?? []).flatMap((project) =>
          project.main_session_id ? [project.main_session_id] : [],
        ),
      ),
    [query.data],
  );
}

/**
 * Other members' persona sessions (the Lead and identities) in a project
 * shared with the viewer, which members can open. Polled while `enabled`.
 */
export function useSharedProjectPersonaRows(
  project: { id: string | null; name: string },
  enabled: boolean,
  viewerId: string | null,
): Conversation[] | undefined {
  const projects = useQuery({
    queryKey: ["projects", "main-sessions"],
    queryFn: listProjects,
    staleTime: 30_000,
  });
  const shared = projects.data?.some(
    (candidate) => candidate.id === project.id && candidate.shared === true,
  );
  const mainSessionIds = useOrchestratorSessionIds();
  const rows = useQuery({
    queryKey: ["project-shared-personas", project.name],
    queryFn: ({ signal }) => fetchSharedProjectSessions(project.name, signal),
    enabled: enabled && shared === true,
    staleTime: 15_000,
    refetchInterval: 30_000,
  }).data;
  return useMemo(
    () =>
      rows?.filter((row) =>
        isSharedProjectPersona(row, project.id, viewerId, mainSessionIds),
      ),
    [rows, project.id, viewerId, mainSessionIds],
  );
}

/** The persona a sidebar session works for, or null for an ordinary session. */
export function sidebarPersonaName(
  session: { id: string; title?: string | null; labels?: Record<string, string> | null },
  orchestratorSessionIds: ReadonlySet<string>,
): string | null {
  if (orchestratorSessionIds.has(session.id)) return "Lead";
  // Identity sessions are titled with their persona's name when they start.
  if (session.labels?.[PROJECT_IDENTITY_LABEL_KEY]) return session.title?.trim() || null;
  return null;
}

/** Look up which persona a project session belongs to, for ticket activity. */
export function useProjectPersonaLookup(projectId: string) {
  const identities = useProjectIdentities(projectId).data;
  return useMemo(() => {
    const bySession = new Map<string, string>();
    const byIdentity = new Map<string, string>();
    for (const identity of identities ?? []) {
      byIdentity.set(identity.id, identity.name);
      if (identity.active_session_id) bySession.set(identity.active_session_id, identity.name);
    }
    /** Current sessions match directly; a recreated persona's older ones match by label. */
    return (sessionId: string | null, labels?: Record<string, string> | null): string | null => {
      if (!sessionId) return null;
      const identityId = labels?.[PROJECT_IDENTITY_LABEL_KEY];
      return bySession.get(sessionId) ?? (identityId ? (byIdentity.get(identityId) ?? null) : null);
    };
  }, [identities]);
}

/**
 * The persona whose session wrote a ticket event. A persona's current session
 * matches at once; an older one (from before it was recreated) matches by its label.
 */
export function usePersonaForSession(
  sessionId: string | null,
  lookup: ReturnType<typeof useProjectPersonaLookup>,
): string | null {
  const client = useQueryClient();
  const direct = lookup(sessionId);
  // Same key as TicketSessionLink's metadata read, so the fetch is shared.
  const session = useQuery({
    queryKey: ["conversation-backfill", sessionId],
    queryFn: () => fetchConversationById(sessionId as string, client),
    initialData: () => (sessionId ? findCachedListRow(client, sessionId) : undefined),
    staleTime: 30_000,
    retry: false,
    enabled: sessionId !== null && direct === null,
  }).data;
  return direct ?? lookup(sessionId, session?.labels);
}
