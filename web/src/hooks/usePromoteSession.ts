import { useMutation, useQueryClient } from "@tanstack/react-query";
import { childSessionsQueryKey } from "@/hooks/useChildSessions";
import { authenticatedFetch } from "@/lib/identity";
import type { Session } from "@/lib/types";

export interface PromoteSessionInput {
  sessionId: string;
  previousParentId: string;
}

async function promoteSession(sessionId: string): Promise<Session> {
  const response = await authenticatedFetch(
    `/v1/sessions/${encodeURIComponent(sessionId)}/promote`,
    { method: "POST" },
  );
  if (!response.ok) {
    const body = (await response.json().catch(() => ({}))) as {
      error?: { message?: string };
    };
    if (response.status === 403) {
      throw new Error("You don't have permission to promote this agent.");
    }
    throw new Error(body.error?.message ?? "Unable to promote this agent.");
  }
  return (await response.json()) as Session;
}

/** Promote one child session and refresh the tree locations it changed. */
export function usePromoteSession() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ sessionId }: PromoteSessionInput) => promoteSession(sessionId),
    onSuccess: async (session, { sessionId, previousParentId }) => {
      queryClient.setQueryData(["session", sessionId], session);
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["conversations"] }),
        queryClient.invalidateQueries({ queryKey: childSessionsQueryKey(previousParentId) }),
        queryClient.invalidateQueries({ queryKey: ["session", sessionId] }),
        queryClient.invalidateQueries({ queryKey: ["rootSessionId"] }),
      ]);
    },
  });
}
