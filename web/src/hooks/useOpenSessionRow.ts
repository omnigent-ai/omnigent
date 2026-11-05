// The open session's sidebar row, for when no loaded list page holds it.

import { type QueryClient, useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useSyncExternalStore } from "react";
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
 * Keep the list row of a session about to be opened, such as a search result,
 * so the sidebar can list it with its owner once that list is gone.
 *
 * @param queryClient - The app QueryClient.
 * @param row - The session's row from the list it was picked from.
 */
export function rememberOpenSessionRow(queryClient: QueryClient, row: Conversation): void {
  queryClient.setQueryData<Conversation | null>(["conversation-backfill", row.id], row);
}

/**
 * The open session's row, read live from any list cache (such as the search it
 * was opened from), else the row kept by ``rememberOpenSessionRow``, else
 * fetched. Rename, pin, move, archive and status changes patch that
 * ``["conversation-backfill", id]`` copy.
 *
 * @param id - Top-level session to reveal, or ``null`` to disable.
 * @returns The row, or ``null`` while loading, when the session is gone, or
 *   when a fetched row has no ``owner`` and the viewer isn't owner-level.
 */
export function useOpenSessionRow(id: string | null): Conversation | null {
  const queryClient = useQueryClient();
  // List rows keep receiving status and attention updates, so a cached one wins.
  const subscribe = useCallback(
    (onChange: () => void) =>
      id === null ? () => undefined : queryClient.getQueryCache().subscribe(onChange),
    [queryClient, id],
  );
  const listRow = useSyncExternalStore(subscribe, () =>
    id === null ? undefined : findCachedListRow(queryClient, id),
  );
  const { data } = useQuery({
    queryKey: ["conversation-backfill", id],
    enabled: id !== null && listRow === undefined,
    staleTime: 30_000,
    retry: false,
    queryFn: async () => {
      const fetched = await fetchConversationById(id as string);
      // The detail response has no owner, so keep the one a remembered list row gave.
      const knownOwner = queryClient.getQueryData<Conversation | null>([
        "conversation-backfill",
        id,
      ])?.owner;
      return fetched && fetched.owner === null && knownOwner
        ? { ...fetched, owner: knownOwner }
        : fetched;
    },
  });
  const row = listRow ?? data;
  if (id === null || !row || row.id !== id) return null;
  if ((row.owner === null || row.owner === undefined) && !isOwnerLevel(row.permission_level)) {
    return null;
  }
  return row;
}
