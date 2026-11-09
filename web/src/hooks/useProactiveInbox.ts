import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback } from "react";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { isFeatureEnabled } from "@/lib/capabilities";
import { withBasePath } from "@/lib/basePath";
import { getOmnigentHostGeneration, getOmnigentServerIdentity } from "@/lib/host";
import {
  applySuggestionUpdate,
  listSuggestions,
  updateSuggestion,
  type SuggestionPage,
  type SuggestionUpdate,
} from "@/lib/proactiveInbox";
import { useRebasePath } from "@/lib/routing";
import { useViewerId } from "./useViewerId";

/** Agent-posted suggestions for the Inbox, when the server offers them. */
export function useProactiveInbox(identityReady: boolean) {
  const info = useServerInfo();
  const viewerId = useViewerId();
  const rebasePath = useRebasePath();
  const queryClient = useQueryClient();
  const queryKey = [
    "proactive-inbox",
    getOmnigentServerIdentity() ?? `mount:${getOmnigentHostGeneration()}`,
    withBasePath(rebasePath("/")),
    viewerId,
  ];
  const enabled = identityReady && isFeatureEnabled(info, "proactive_agent");
  const query = useQuery({
    queryKey,
    queryFn: listSuggestions,
    enabled,
    staleTime: 60_000,
    // Suggestions arrive from scheduled runs, so a slow poll is enough.
    refetchInterval: 300_000,
    retry: 1,
  });
  const queryKeyText = JSON.stringify(queryKey);
  const update = useCallback(
    (id: string, change: SuggestionUpdate) => {
      const key = JSON.parse(queryKeyText) as unknown[];
      queryClient.setQueryData<SuggestionPage>(key, (page) =>
        page ? applySuggestionUpdate(page, id, change) : page,
      );
      void updateSuggestion(id, change).catch(() => {
        void queryClient.invalidateQueries({ queryKey: key });
      });
    },
    [queryClient, queryKeyText],
  );
  return {
    items: enabled ? (query.data?.data ?? []) : [],
    unreadCount: enabled ? (query.data?.unreadCount ?? 0) : 0,
    isLoading: enabled && query.isLoading,
    isError: enabled && query.isError,
    refetch: query.refetch,
    update,
  };
}
