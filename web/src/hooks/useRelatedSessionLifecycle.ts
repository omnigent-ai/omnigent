import { useMutation, useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { isFeatureEnabled } from "@/lib/capabilities";
import { authenticatedFetch } from "@/lib/identity";
import { apiErrorFromResponse } from "@/lib/sessionsApi";
import { markRecentlyCreated } from "@/lib/sessionListCache";
import { applyConfirmedArchive, type Conversation } from "./useConversations";
import { childSessionsQueryKey, fetchChildSessions } from "./useChildSessions";
import { isTempConvId } from "@/lib/tempConversationId";

export function useRelatedSessionGroup(sessionId: string, active = true) {
  const enabled = isFeatureEnabled(useServerInfo(), "related_sessions");
  const {
    data: children = [],
    isPending,
    error,
  } = useQuery({
    queryKey: childSessionsQueryKey(sessionId),
    queryFn: () => fetchChildSessions(sessionId, true),
    enabled: enabled && active && !isTempConvId(sessionId),
    staleTime: 60_000,
    retry: false,
  });
  return {
    enabled,
    hasPeers:
      enabled && children.some((child) => child.labels?.["omnigent.session_role"] === "peer"),
    loading: enabled && isPending,
    error: enabled ? error : null,
  };
}

type ArchiveResult = Conversation & {
  parent_session_id?: string | null;
  archive_changed_session_ids: string[];
};

export async function archiveRelatedSessions(
  id: string,
  archived: boolean,
  restoreIds?: string[],
): Promise<ArchiveResult> {
  const response = await authenticatedFetch(
    `/v1/sessions/${encodeURIComponent(id)}?include_usage=false`,
    {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        archived,
        archive_scope: "tree",
        ...(restoreIds ? { archive_session_ids: restoreIds } : {}),
      }),
    },
  );
  if (!response.ok) throw await apiErrorFromResponse(response);
  return response.json();
}

async function applyArchiveResult(
  queryClient: QueryClient,
  result: ArchiveResult,
  archived: boolean,
) {
  const changed = new Set(result.archive_changed_session_ids);
  await applyConfirmedArchive(queryClient, [...changed], archived);
  // Only the root belongs in sidebar caches; peers stay under their root.
  if (!archived && changed.has(result.id) && !result.parent_session_id) markRecentlyCreated(result);
  void queryClient.invalidateQueries({ queryKey: ["conversations"] });
}

/** Group archive leaves shared worktrees intact; Undo restores only changed IDs. */
export function useArchiveRelatedSessions() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({
      conversation,
      archived,
    }: {
      conversation: Pick<Conversation, "id">;
      archived: boolean;
    }) => {
      const startedAt = Date.now();
      return { result: await archiveRelatedSessions(conversation.id, archived), startedAt };
    },
    onSuccess: async ({ result, startedAt }, { archived }) => {
      await applyArchiveResult(queryClient, result, archived);
      if (!archived || result.archive_changed_session_ids.length === 0) return;
      // Count response latency against the existing server teardown grace.
      const remaining = Math.max(0, 3000 - (Date.now() - startedAt));
      const count = result.archive_changed_session_ids.length;
      toast(`Archived ${count} ${count === 1 ? "session" : "sessions"}`, {
        duration: remaining || 3000,
        ...(remaining > 0
          ? {
              action: {
                label: "Undo",
                onClick: () => {
                  void archiveRelatedSessions(result.id, false, result.archive_changed_session_ids)
                    .then((restored) => applyArchiveResult(queryClient, restored, false))
                    .catch(() => toast.error("Couldn't restore the sessions."));
                },
              },
            }
          : {}),
      });
    },
    onError: (error) =>
      toast.error(error instanceof Error ? error.message : "Couldn't update the sessions."),
  });
}
