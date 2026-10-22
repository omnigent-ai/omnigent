import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  getOrchestrationSetting,
  setOrchestrationSetting,
  listSessionLinks,
} from "@/lib/sessionLinksApi";

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

export function useOrchestrationSetting(enabled: boolean) {
  return useQuery({
    queryKey: [...SESSION_LINKS_KEY, "owner-setting"],
    queryFn: ({ signal }) => getOrchestrationSetting(signal),
    enabled,
  });
}

export function useChangeOrchestrationSetting() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: setOrchestrationSetting,
    onSuccess: (setting) => {
      client.setQueryData([...SESSION_LINKS_KEY, "owner-setting"], setting);
      return client.invalidateQueries({ queryKey: SESSION_LINKS_KEY });
    },
    onError: () => client.invalidateQueries({ queryKey: [...SESSION_LINKS_KEY, "owner-setting"] }),
  });
}
