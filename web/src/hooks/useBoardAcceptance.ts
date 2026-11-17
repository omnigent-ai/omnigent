import { useIsMutating, useMutation, useQueryClient } from "@tanstack/react-query";
import { showToast } from "@/components/ui/toast";
import { authenticatedFetch } from "@/lib/identity";
import { BOARD_ACCEPTED_LABEL_KEY } from "@/lib/sessionBoard";
import { ApiError, apiErrorFromResponse } from "@/lib/sessionsApi";
import { LIST_ROW_QUERY_ROOTS } from "@/lib/sessionListCache";
import {
  expireLabelWrite,
  isLatestLabelWrite,
  markLabelWrite,
  overlayLabelIntoCaches,
  PINNED_CONVERSATIONS_KEY,
  unmarkLabelWrite,
  type CachedLabelCopy,
  type Conversation,
  type LabelWriteTombstone,
} from "./useConversations";
import { fetchConversationById } from "./useOpenSessionRow";

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

interface BoardAcceptContext {
  /** This write's tombstone; the write is current while it is the latest one. */
  entry: LabelWriteTombstone;
  /** The label written optimistically, or `null` when a newer write came first. */
  optimistic: string | null;
}

/**
 * Whether a cached copy shows the session state the user acted on: the same
 * `board_revision`, or for open-session rows without one, the same activity
 * time. The `["session", id]` snapshot has neither, so only label checks guard it.
 */
function atActedOnState(copy: CachedLabelCopy, row: Conversation): boolean {
  if (copy.board_revision != null) return copy.board_revision === row.board_revision;
  if (copy.updated_at !== undefined) return copy.updated_at === row.updated_at;
  return true;
}

/** A failure that leaves unknown whether the server stored the write. */
function mayHaveCommitted(error: unknown): boolean {
  return !(error instanceof ApiError) || error.status >= 500;
}

/**
 * Accept a session's work on the board, or reopen it. The card moves before the
 * PATCH returns: the overlay writes the stamp the server stores for an unchanged
 * session, and a tombstone keeps it on lists and stream rows that still show the
 * old label. On success the response's label is applied exactly, including its
 * absence, which means later activity already reopened the session. A refusal
 * restores the previous value and says why; a failure that may have been stored
 * reads the session back first. Outcomes apply only while this is the session's
 * latest write, and only to copies still at the acted-on state, so newer
 * activity wins. A label PATCH leaves the session's `updated_at` alone, so
 * accepting does not mark it as new activity.
 */
export function useSetBoardAccepted() {
  const queryClient = useQueryClient();
  // Write to copies at the acted-on state whose current label passes `holds`.
  const writeLabel = (
    row: Conversation,
    value: string | undefined,
    holds: (label: string | undefined) => boolean,
  ) =>
    overlayLabelIntoCaches(
      queryClient,
      row.id,
      BOARD_ACCEPTED_LABEL_KEY,
      value,
      (copy) => atActedOnState(copy, row) && holds(copy.labels?.[BOARD_ACCEPTED_LABEL_KEY]),
    );
  // Apply the server's label, including to a copy an older read put back.
  const settle = (row: Conversation, context: BoardAcceptContext, value: string | undefined) => {
    const settled = { ...context.entry, value, landed: true };
    markLabelWrite(row.id, settled);
    expireLabelWrite(row.id, settled);
    writeLabel(
      row,
      value,
      (label) => label === context.optimistic || label === context.entry.before,
    );
  };
  const rollBack = (row: Conversation, context: BoardAcceptContext) => {
    unmarkLabelWrite(row.id, context.entry);
    writeLabel(row, context.entry.before, (label) => label === context.optimistic);
  };
  return useMutation({
    mutationKey: BOARD_ACCEPT_MUTATION_KEY,
    mutationFn: ({ row, reviewedRevision }: BoardAcceptVariables) =>
      setSessionBoardAccepted(row.id, reviewedRevision),
    onMutate: async ({ row, reviewedRevision }): Promise<BoardAcceptContext> => {
      const before = row.labels?.[BOARD_ACCEPTED_LABEL_KEY];
      const optimistic = reviewedRevision === null ? "" : String(row.updated_at + 1);
      const entry: LabelWriteTombstone = {
        key: BOARD_ACCEPTED_LABEL_KEY,
        before,
        value: optimistic,
        boardRevision: row.board_revision,
        updatedAt: row.updated_at,
      };
      markLabelWrite(row.id, entry);
      // A refetch already in flight, of any cache the overlay writes, would land
      // without the new label.
      await Promise.all([
        ...LIST_ROW_QUERY_ROOTS.map((root) => queryClient.cancelQueries({ queryKey: [root] })),
        queryClient.cancelQueries({ queryKey: PINNED_CONVERSATIONS_KEY }),
      ]);
      if (!isLatestLabelWrite(row.id, entry)) return { entry, optimistic: null };
      writeLabel(row, optimistic, (label) => label === before);
      return { entry, optimistic };
    },
    onSuccess: (updated, { row }, context) => {
      if (!context || !isLatestLabelWrite(row.id, context.entry)) return;
      settle(row, context, updated.labels?.[BOARD_ACCEPTED_LABEL_KEY]);
    },
    onError: async (error, { row, reviewedRevision }, context) => {
      const refused = error instanceof ApiError && error.status === 409;
      // A newer write to this session owns its label and its outcome.
      if (context && isLatestLabelWrite(row.id, context.entry)) {
        // The detail read comes from the database, not the lagging list index.
        const current = mayHaveCommitted(error)
          ? await fetchConversationById(row.id, queryClient).catch(() => null)
          : null;
        if (!isLatestLabelWrite(row.id, context.entry)) return;
        const value = current?.labels[BOARD_ACCEPTED_LABEL_KEY];
        if (current && value !== context.entry.before) {
          settle(row, context, value);
          return;
        }
        rollBack(row, context);
        if (reviewedRevision === null) showToast("Couldn't reopen the session.");
        else if (refused) showToast(error.message);
        else showToast("Couldn't accept the session.");
      }
      // A refused Accept needs the current revision for a retry. The Pinned
      // list's index lags writes, so it is left to converge on its own.
      if (refused)
        for (const root of LIST_ROW_QUERY_ROOTS)
          void queryClient.invalidateQueries({ queryKey: [root] });
    },
  });
}
