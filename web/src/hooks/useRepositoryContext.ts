import { useCallback } from "react";
import { useQuery } from "@tanstack/react-query";

import { isTempConvId } from "@/lib/tempConversationId";
import { workspaceSidebarSource } from "@/lib/workspaceSidebarSource";

export function repositoryContextKey(conversationId: string | undefined) {
  return ["repository-context", conversationId] as const;
}

/**
 * The session workspace's branch, HEAD, push target and remotes, from its host; `data` is
 * undefined when no route's host serves the method. A push, commit or checkout changes the
 * answer without any other signal, so `refresh` reads it again, as when a menu offering it
 * opens; focusing the page does too.
 */
export function useRepositoryContext(rawConversationId: string | undefined, enabled = true) {
  // A `temp:*` id (navigate-first new-chat window) has no server session.
  const conversationId = isTempConvId(rawConversationId) ? undefined : rawConversationId;
  const active = !!conversationId && enabled;
  const { data, refetch } = useQuery({
    queryKey: repositoryContextKey(conversationId),
    queryFn: ({ signal }) =>
      workspaceSidebarSource().resolveRepositoryContext(conversationId!, signal),
    enabled: active,
    staleTime: 15_000,
    refetchOnWindowFocus: true,
    retry: false,
  });
  // `refetch` ignores `enabled`, so a disabled query stays unread.
  const refresh = useCallback(() => {
    if (active) void refetch();
  }, [active, refetch]);
  return { data, refresh };
}
