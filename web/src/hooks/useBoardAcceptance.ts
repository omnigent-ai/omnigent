import {
  type QueryClient,
  useIsMutating,
  useMutation,
  useQueryClient,
} from "@tanstack/react-query";
import { showToast } from "@/components/ui/toast";
import { authenticatedFetch } from "@/lib/identity";
import { BOARD_ACCEPTED_LABEL_KEY } from "@/lib/sessionBoard";
import { ApiError, apiErrorFromResponse } from "@/lib/sessionsApi";
import { LIST_ROW_QUERY_ROOTS } from "@/lib/sessionListCache";
import {
  overlayLabelIntoCaches,
  PINNED_CONVERSATIONS_KEY,
  type CachedLabelCopy,
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

// The latest Accept/Reopen started per session, so an older write that settles
// after a newer one leaves the newer one's label alone.
const latestBoardWrites = new WeakMap<QueryClient, Map<string, symbol>>();

function latestWrites(queryClient: QueryClient): Map<string, symbol> {
  let writes = latestBoardWrites.get(queryClient);
  if (!writes) latestBoardWrites.set(queryClient, (writes = new Map()));
  return writes;
}

interface BoardAcceptContext {
  operation: symbol;
  /** The label value written optimistically, or `null` when none was written. */
  optimistic: string | null;
}

/** A cached copy still shows the session state the user acted on. */
function atReviewedRevision(copy: CachedLabelCopy, row: Conversation): boolean {
  return copy.board_revision == null || copy.board_revision === row.board_revision;
}

/**
 * Accept a session's work on the board, or reopen it. The card moves before the
 * PATCH returns: the overlay writes the stamp the server stores for an unchanged
 * session. On success the response's label is applied exactly, including its
 * absence, which means later activity already reopened the session. A failure
 * restores the previous value and says why. Either outcome lands only on copies
 * still holding this write's optimistic label at the acted-on revision, and only
 * while no newer write to the session started, so newer streamed activity wins.
 * The lists refetch when the outcome is uncertain: a failure the server may have
 * committed anyway, or a copy at that revision that disagrees with the response.
 * A label PATCH leaves the session's `updated_at` alone, so accepting does not
 * mark it as new activity.
 */
export function useSetBoardAccepted() {
  const queryClient = useQueryClient();
  const refetchLists = () => {
    for (const queryKey of [
      ...LIST_ROW_QUERY_ROOTS.map((root) => [root]),
      PINNED_CONVERSATIONS_KEY,
    ])
      void queryClient.invalidateQueries({ queryKey });
  };
  // Apply this write's outcome if it is still the session's latest write, and
  // report whether a copy at the acted-on revision holds some other label.
  const settle = (
    row: Conversation,
    context: BoardAcceptContext | undefined,
    value: string | undefined,
  ): boolean => {
    const writes = latestWrites(queryClient);
    if (!context || writes.get(row.id) !== context.operation) return false;
    writes.delete(row.id);
    const { optimistic } = context;
    let disagrees = false;
    overlayLabelIntoCaches(queryClient, row.id, BOARD_ACCEPTED_LABEL_KEY, value, (copy) => {
      if (!atReviewedRevision(copy, row)) return false;
      const label = copy.labels?.[BOARD_ACCEPTED_LABEL_KEY];
      if (label === optimistic) return true;
      if (label !== value) disagrees = true;
      return false;
    });
    return disagrees;
  };
  return useMutation({
    mutationKey: BOARD_ACCEPT_MUTATION_KEY,
    mutationFn: ({ row, reviewedRevision }: BoardAcceptVariables) =>
      setSessionBoardAccepted(row.id, reviewedRevision),
    onMutate: async ({ row, reviewedRevision }): Promise<BoardAcceptContext> => {
      const operation = Symbol(row.id);
      const writes = latestWrites(queryClient);
      writes.set(row.id, operation);
      // A refetch already in flight, of any cache the overlay writes, would land
      // without the new label.
      await Promise.all([
        ...LIST_ROW_QUERY_ROOTS.map((root) => queryClient.cancelQueries({ queryKey: [root] })),
        queryClient.cancelQueries({ queryKey: PINNED_CONVERSATIONS_KEY }),
      ]);
      if (writes.get(row.id) !== operation) return { operation, optimistic: null };
      const optimistic = reviewedRevision === null ? "" : String(row.updated_at + 1);
      overlayLabelIntoCaches(queryClient, row.id, BOARD_ACCEPTED_LABEL_KEY, optimistic, (copy) =>
        atReviewedRevision(copy, row),
      );
      return { operation, optimistic };
    },
    onSuccess: (updated, { row }, context) => {
      // A refetch that landed before the write committed left a copy behind.
      if (settle(row, context, updated.labels?.[BOARD_ACCEPTED_LABEL_KEY])) refetchLists();
    },
    onError: (error, { row, reviewedRevision }, context) => {
      settle(row, context, row.labels?.[BOARD_ACCEPTED_LABEL_KEY]);
      // The write may have committed before it failed, and a refused Accept
      // needs the current revision so a retry reviews the session as it is now.
      refetchLists();
      if (reviewedRevision === null) showToast("Couldn't reopen the session.");
      else if (error instanceof ApiError && error.status === 409) showToast(error.message);
      else showToast("Couldn't accept the session.");
    },
  });
}
