import { useQuery } from "@tanstack/react-query";

import { isTempConvId } from "@/lib/tempConversationId";
import { workspaceSidebarSource } from "@/lib/workspaceSidebarSource";

export function repositoryContextKey(conversationId: string | undefined) {
  return ["repository-context", conversationId] as const;
}

/**
 * The session workspace's branch, HEAD, push target and remotes, from its host. Errors when
 * no route's host serves the method; a push usually happens outside the page, so focusing
 * it reads again.
 */
export function useRepositoryContext(rawConversationId: string | undefined, enabled = true) {
  // A `temp:*` id (navigate-first new-chat window) has no server session.
  const conversationId = isTempConvId(rawConversationId) ? undefined : rawConversationId;
  return useQuery({
    queryKey: repositoryContextKey(conversationId),
    queryFn: ({ signal }) =>
      workspaceSidebarSource().resolveRepositoryContext(conversationId!, signal),
    enabled: !!conversationId && enabled,
    staleTime: 15_000,
    refetchOnWindowFocus: true,
    retry: false,
  });
}
