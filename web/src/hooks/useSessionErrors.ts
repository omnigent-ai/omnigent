import { useCallback, useSyncExternalStore } from "react";
import { useQueries, useQueryClient } from "@tanstack/react-query";
import type { Conversation } from "./useConversations";
import { itemsToBlocks } from "@/lib/itemsToBlocks";
import { latestActivityIsError } from "@/lib/sessionError";
import { fetchSessionItemsPage } from "@/lib/sessionsApi";
import { isTempConvId } from "@/lib/tempConversationId";
import { conversationRegistry } from "@/store/conversationRegistry";

type ErrorConversation = Pick<
  Conversation,
  "id" | "updated_at" | "status" | "pending_elicitations_count" | "provisional"
>;

// Leave connections available for navigation and the existing chat streams.
let activeReads = 0;
const waitingReads: (() => void)[] = [];

async function fetchLatestError(id: string, signal: AbortSignal): Promise<boolean> {
  await new Promise<void>((resolve) => {
    if (activeReads < 2) {
      activeReads += 1;
      resolve();
    } else {
      waitingReads.push(resolve);
    }
  });
  try {
    signal.throwIfAborted();
    const page = await fetchSessionItemsPage(id, { limit: 1, signal });
    const error = latestActivityIsError(itemsToBlocks(page.items));
    if (error !== undefined || !page.hasMore) return error ?? false;
    // A hidden metadata item may trail the last visible message. Bound the
    // fallback instead of hydrating a whole transcript just for its badge.
    const older = await fetchSessionItemsPage(id, {
      olderThan: page.items[0]?.id,
      limit: 8,
      signal,
    });
    return latestActivityIsError(itemsToBlocks(older.items)) ?? false;
  } finally {
    const next = waitingReads.shift();
    if (next) next();
    else activeReads -= 1;
  }
}

function liveError(id: string): boolean | undefined {
  const entry = conversationRegistry.peek(id);
  const state = entry?.getState();
  if (entry?.disposed || !state || state.conversationLoadError !== null) return undefined;
  // The initial history request will supply the latest message. Wait for it
  // even before the stream controller is installed, without a second read.
  if (state.loadingConversation) return false;
  if (state.abortController === null || state.abortController.signal.aborted) {
    return undefined;
  }
  if (state.status === "streaming" || state.terminalPending) return false;
  return latestActivityIsError(state.blocks) ?? false;
}

/** Reuse live transcripts; unopened rows share a cached, bounded tail read. */
export function useSessionErrors(conversations: readonly ErrorConversation[]): boolean[] {
  const queryClient = useQueryClient();
  const subscribe = useCallback(
    (notify: () => void) => {
      const ids = new Set(conversations.map((c) => c.id));
      const changed = (id: string) => {
        if (!ids.has(id)) return;
        if (liveError(id) === undefined) {
          // A disposed/dead stream may have superseded a cached tail while
          // live. Revalidate before relying on that old cache again.
          void queryClient.invalidateQueries({
            queryKey: ["session-latest-error", id],
            refetchType: "none",
          });
        }
        notify();
      };
      const unsubscribe = conversationRegistry.subscribe(changed);
      const unsubscribeDisposed = conversationRegistry.subscribeDisposed(changed);
      return () => {
        unsubscribe();
        unsubscribeDisposed();
      };
    },
    [conversations, queryClient],
  );
  // A primitive snapshot only notifies React when an error actually changes,
  // not on every streamed token or unrelated chat-store update.
  const getSnapshot = useCallback(
    () =>
      conversations
        .map((c) => {
          const error = liveError(c.id);
          return error === undefined ? "?" : error ? "1" : "0";
        })
        .join(""),
    [conversations],
  );
  const live = useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
  const queries = useQueries({
    queries: conversations.map((c, i) => ({
      // updated_at is the same freshness signal used by the unread dot and
      // refreshed by the shared session-updates socket/list reconciliation.
      queryKey: ["session-latest-error", c.id, c.updated_at, c.status],
      queryFn: ({ signal }: { signal: AbortSignal }) => fetchLatestError(c.id, signal),
      enabled:
        live[i] === "?" &&
        !isTempConvId(c.id) &&
        !c.provisional &&
        c.status !== "running" &&
        c.status !== "failed" &&
        (c.pending_elicitations_count ?? 0) === 0,
      staleTime: Infinity,
      retry: false,
    })),
  });
  return queries.map((query, i) => (live[i] === "?" ? (query.data ?? false) : live[i] === "1"));
}
