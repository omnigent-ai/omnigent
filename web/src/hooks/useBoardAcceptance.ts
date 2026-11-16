import { useIsMutating, useMutation, useQueryClient } from "@tanstack/react-query";
import { showToast } from "@/components/ui/toast";
import { authenticatedFetch } from "@/lib/identity";
import { BOARD_ACCEPTED_LABEL_KEY } from "@/lib/sessionBoard";
import { ApiError, apiErrorFromResponse } from "@/lib/sessionsApi";
import { LIST_ROW_QUERY_ROOTS } from "@/lib/sessionListCache";
import {
  overlayLabelIntoCaches,
  PINNED_CONVERSATIONS_KEY,
  type Conversation,
} from "./useConversations";

/**
 * PATCH the board acceptance label. `reviewedRevision` is the session's
 * `board_revision` the user reviewed; the server refuses it (409) once the
 * session has changed, and otherwise stores a stamp one second past its
 * `updated_at`. `null` reopens the session by clearing the label to an empty
 * value (labels are upsert-only).
 */
export async function setSessionBoardAccepted(
  id: string,
  reviewedRevision: string | null,
): Promise<Conversation> {
  const res = await authenticatedFetch(`/v1/sessions/${encodeURIComponent(id)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      labels: { [BOARD_ACCEPTED_LABEL_KEY]: reviewedRevision ?? "" },
    }),
  });
  if (!res.ok) throw await apiErrorFromResponse(res);
  return (await res.json()) as Conversation;
}

const BOARD_ACCEPT_MUTATION_KEY = ["board-accept"] as const;

interface BoardAcceptVariables {
  row: Conversation;
  /** The `board_revision` the user reviewed, or `null` to reopen. */
  reviewedRevision: string | null;
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
 * Accept a session's work on the board, or reopen it. The card moves before the
 * PATCH returns: the overlay writes the stamp the server stores for an unchanged
 * session. On success the response's label is applied exactly, including its
 * absence, which means later activity already reopened the session. A failure
 * restores the previous value and says why. A label PATCH leaves the session's
 * `updated_at` alone, so accepting does not mark it as new activity.
 */
export function useSetBoardAccepted() {
  const queryClient = useQueryClient();
  const writeLabel = (id: string, value: string | undefined) =>
    overlayLabelIntoCaches(queryClient, id, BOARD_ACCEPTED_LABEL_KEY, value);
  return useMutation({
    mutationKey: BOARD_ACCEPT_MUTATION_KEY,
    mutationFn: ({ row, reviewedRevision }: BoardAcceptVariables) =>
      setSessionBoardAccepted(row.id, reviewedRevision),
    onMutate: async ({ row, reviewedRevision }) => {
      // A refetch already in flight, of any cache the overlay writes, would land
      // without the new label.
      await Promise.all([
        ...LIST_ROW_QUERY_ROOTS.map((root) => queryClient.cancelQueries({ queryKey: [root] })),
        queryClient.cancelQueries({ queryKey: PINNED_CONVERSATIONS_KEY }),
      ]);
      writeLabel(row.id, reviewedRevision === null ? "" : String(row.updated_at + 1));
    },
    onSuccess: (updated, { row }) => {
      writeLabel(row.id, updated.labels?.[BOARD_ACCEPTED_LABEL_KEY]);
    },
    onError: (error, { row, reviewedRevision }) => {
      writeLabel(row.id, row.labels?.[BOARD_ACCEPTED_LABEL_KEY]);
      if (reviewedRevision === null) showToast("Couldn't reopen the session.");
      else if (error instanceof ApiError && error.status === 409) {
        showToast(error.message);
        // Fetch the current revision so a retry reviews the session as it is now.
        for (const root of LIST_ROW_QUERY_ROOTS)
          void queryClient.invalidateQueries({ queryKey: [root] });
      } else showToast("Couldn't accept the session.");
    },
  });
}
