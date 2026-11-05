// The open session's sidebar row, for when no loaded list page holds it.

import { useQuery, useQueryClient } from "@tanstack/react-query";
import type { Conversation } from "@/hooks/useConversations";
import { authenticatedFetch } from "@/lib/identity";
import { isOwnerLevel } from "@/lib/permissionsApi";
import { sessionRoutingHostId, setSessionHost, setSessionParent } from "@/lib/sessionHost";
import { findCachedListRow } from "@/lib/sessionListCache";

/**
 * Fetch a single session as a sidebar-shaped ``Conversation`` object.
 *
 * Returns ``null`` on 404 (session deleted). The single-session response has
 * no ``owner``, so callers classify ownership from ``permission_level``.
 *
 * @param id - Session id, e.g. ``"conv_abc123"``.
 */
export async function fetchConversationById(id: string): Promise<Conversation | null> {
  const params = new URLSearchParams({ include_items: "false", include_usage: "false" });
  const res = await authenticatedFetch(`/v1/sessions/${encodeURIComponent(id)}?${params}`);
  if (res.status === 404) return null;
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  const wire = await res.json();
  // This row can be the only way the session enters client state, and
  // session-scoped requests key their replica off the host map.
  setSessionHost(wire.id, sessionRoutingHostId(wire.host_id, wire.labels));
  setSessionParent(wire.id, wire.parent_session_id, wire.labels);
  return {
    id: wire.id,
    object: "conversation",
    title: wire.title ?? null,
    created_at: wire.created_at,
    updated_at: wire.updated_at ?? wire.created_at,
    labels: wire.labels ?? {},
    permission_level: wire.permission_level ?? null,
    owner: wire.owner ?? null,
    runner_id: wire.runner_id ?? null,
    host_id: wire.host_id ?? null,
    workspace: wire.workspace ?? null,
    agent_id: wire.agent_id,
    agent_name: wire.agent_name ?? null,
    harness: wire.harness ?? null,
    pending_elicitations_count: wire.pending_elicitations_count ?? 0,
    status: wire.status ?? "idle",
    runner_online: wire.runner_online ?? undefined,
    host_online: wire.host_online ?? undefined,
    git_branch: wire.git_branch ?? null,
    archived: wire.archived ?? false,
    parent_session_id: wire.parent_session_id ?? null,
    project_id: wire.project_id ?? null,
  };
}

/**
 * The open session's row, reused from any list cache (such as the search it
 * was opened from) or fetched. Rename, pin, move and archive already patch the
 * ``["conversation-backfill", id]`` key, so the row follows those edits.
 *
 * @param id - Top-level session to reveal, or ``null`` to disable.
 * @returns The row, or ``null`` while loading, when the session is gone, or
 *   when a fetched row has no ``owner`` and the viewer isn't owner-level.
 */
export function useOpenSessionRow(id: string | null): Conversation | null {
  const queryClient = useQueryClient();
  const { data } = useQuery({
    queryKey: ["conversation-backfill", id],
    enabled: id !== null,
    staleTime: 30_000,
    retry: false,
    queryFn: async () =>
      findCachedListRow(queryClient, id as string) ?? (await fetchConversationById(id as string)),
  });
  if (id === null || !data || data.id !== id) return null;
  if (data.owner == null && !isOwnerLevel(data.permission_level)) return null;
  return data;
}
