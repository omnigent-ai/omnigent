import { useInfiniteQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { connectSessions, disconnectSessions, listSessionLinks } from "@/lib/sessionLinksApi";

export const SESSION_LINKS_KEY = ["session-links"] as const;

export function useSessionLinks(sessionId: string, incoming: boolean, enabled: boolean) {
  return useInfiniteQuery({
    queryKey: [...SESSION_LINKS_KEY, sessionId, incoming],
    queryFn: ({ pageParam, signal }) => listSessionLinks(sessionId, incoming, pageParam, signal),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (page) => page.next_cursor ?? undefined,
    enabled,
    refetchInterval: enabled ? 15_000 : false,
  });
}

export function useChangeSessionLink() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({
      sourceId,
      targetId,
      disconnect,
    }: {
      sourceId: string;
      targetId: string;
      disconnect?: boolean;
    }) =>
      disconnect ? disconnectSessions(sourceId, targetId) : connectSessions(sourceId, targetId),
    onSuccess: () => client.invalidateQueries({ queryKey: SESSION_LINKS_KEY }),
  });
}
