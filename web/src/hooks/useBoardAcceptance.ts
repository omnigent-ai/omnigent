import { useIsMutating, useMutation, useQueryClient } from "@tanstack/react-query";
import { showToast } from "@/components/ui/toast";
import { authenticatedFetch } from "@/lib/identity";
import { BOARD_ACCEPTED_LABEL_KEY } from "@/lib/sessionBoard";
import { apiErrorFromResponse } from "@/lib/sessionsApi";
import { LIST_ROW_QUERY_ROOTS } from "@/lib/sessionListCache";
import { overlayLabelIntoCaches, type Conversation } from "./useConversations";

/**
 * PATCH the board acceptance label. `acceptedAt` is the session's `updated_at`
 * being accepted; `null` reopens the session by clearing the label to an empty
 * value (labels are upsert-only).
 */
export async function setSessionBoardAccepted(
  id: string,
  acceptedAt: number | null,
): Promise<Conversation> {
  const res = await authenticatedFetch(`/v1/sessions/${encodeURIComponent(id)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      labels: { [BOARD_ACCEPTED_LABEL_KEY]: acceptedAt === null ? "" : String(acceptedAt) },
    }),
  });
  if (!res.ok) throw await apiErrorFromResponse(res);
  return (await res.json()) as Conversation;
}

const BOARD_ACCEPT_MUTATION_KEY = ["board-accept"] as const;

interface BoardAcceptVariables {
  row: Conversation;
  acceptedAt: number | null;
}

/**
 * Whether an Accept or Reopen for this session is still in flight. Read through
 * the mutation cache, so a card that moved columns (and remounted) still sees it.
 */
export function useBoardAcceptPending(id: string): boolean {
  return (
    useIsMutating({
      mutationKey: BOARD_ACCEPT_MUTATION_KEY,
      predicate: (mutation) =>
        (mutation.state.variables as BoardAcceptVariables | undefined)?.row.id === id,
    }) > 0
  );
}

/**
 * Accept a session's work on the board, or reopen it. The label lands in the
 * cached rows immediately so the card moves before the PATCH returns; a
 * failure restores the previous value and says so. A label PATCH leaves the
 * session's `updated_at` alone, so accepting does not mark it as new activity.
 */
export function useSetBoardAccepted() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationKey: BOARD_ACCEPT_MUTATION_KEY,
    mutationFn: ({ row, acceptedAt }: BoardAcceptVariables) =>
      setSessionBoardAccepted(row.id, acceptedAt),
    onMutate: async ({ row, acceptedAt }) => {
      // A list refetch already in flight would land without the new label.
      await Promise.all(
        LIST_ROW_QUERY_ROOTS.map((root) => queryClient.cancelQueries({ queryKey: [root] })),
      );
      overlayLabelIntoCaches(
        queryClient,
        row.id,
        BOARD_ACCEPTED_LABEL_KEY,
        acceptedAt === null ? "" : String(acceptedAt),
      );
    },
    onError: (_error, { row, acceptedAt }) => {
      overlayLabelIntoCaches(
        queryClient,
        row.id,
        BOARD_ACCEPTED_LABEL_KEY,
        row.labels?.[BOARD_ACCEPTED_LABEL_KEY],
      );
      showToast(
        acceptedAt === null ? "Couldn't reopen the session." : "Couldn't accept the session.",
      );
    },
  });
}
