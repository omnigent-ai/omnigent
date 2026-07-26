import { useMutation, useQueryClient } from "@tanstack/react-query";
import { type Conversation, PINNED_CONVERSATIONS_KEY } from "@/hooks/useConversations";
import { authenticatedFetch } from "@/lib/identity";
import { COMPLETED_LABEL_KEY, type ConversationsInfiniteData } from "@/lib/sessionListCache";

export async function setConversationCompleted(
  id: string,
  completed: boolean,
): Promise<Conversation> {
  const res = await authenticatedFetch(`/v1/sessions/${encodeURIComponent(id)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      labels: { [COMPLETED_LABEL_KEY]: completed ? String(Date.now()) : "" },
    }),
  });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return (await res.json()) as Conversation;
}

export function useToggleCompletedConversation() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({ id, completed }: { id: string; completed: boolean }) =>
      setConversationCompleted(id, completed),
    onSuccess: (updated) => {
      const patchPages = (data: ConversationsInfiniteData | undefined) =>
        data
          ? {
              ...data,
              pages: data.pages.map((page) => ({
                ...page,
                data: page.data.map((conversation) =>
                  conversation.id === updated.id
                    ? { ...conversation, labels: updated.labels }
                    : conversation,
                ),
              })),
            }
          : data;
      for (const queryKey of [["conversations"], ["project-sessions"]]) {
        queryClient.setQueriesData<ConversationsInfiniteData>({ queryKey }, patchPages);
      }
      queryClient.setQueryData<Conversation[]>(PINNED_CONVERSATIONS_KEY, (old) =>
        old?.map((conversation) =>
          conversation.id === updated.id
            ? { ...conversation, labels: updated.labels }
            : conversation,
        ),
      );
    },
  });
}
