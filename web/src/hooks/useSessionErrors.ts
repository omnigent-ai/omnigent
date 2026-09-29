import { useCallback, useSyncExternalStore } from "react";
import { useQueries, useQueryClient } from "@tanstack/react-query";
import type { Conversation } from "./useConversations";
import { itemsToBlocks } from "@/lib/itemsToBlocks";
import {
  latestActivityErrorState,
  latestActivityErrorWindow,
  type LatestSessionError,
} from "@/lib/sessionError";
import { fetchSessionItemsPage, type SessionItemsPage } from "@/lib/sessionsApi";
import { isStaleCursorError } from "@/lib/staleCursor";
import { isTempConvId } from "@/lib/tempConversationId";
import { conversationRegistry } from "@/store/conversationRegistry";

type ErrorConversation = Pick<
  Conversation,
  "id" | "updated_at" | "status" | "pending_elicitations_count" | "provisional" | "host_online"
>;

// Leave connections available for navigation and the existing chat streams.
let activeReads = 0;
const waitingReads: (() => void)[] = [];

async function fetchLatestError(
  id: string,
  hostOnline: boolean | null | undefined,
  signal: AbortSignal,
): Promise<LatestSessionError | null> {
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
    const latest = latestActivityErrorWindow(itemsToBlocks(page.items), hostOnline);
    const needsOlderBoundary =
      (latest.state === "disconnected" || latest.state === "recovered_disconnect") &&
      !latest.boundaryResolved;
    if ((latest.state !== undefined && !needsOlderBoundary) || !page.hasMore) {
      return latest.state ?? null;
    }
    // A hidden metadata item may trail the last visible message, and a runner
    // disconnect may trail a genuine fault in the same response. Bound the
    // fallback instead of hydrating a whole transcript just for its badge.
    let older: SessionItemsPage;
    try {
      older = await fetchSessionItemsPage(id, {
        olderThan: page.items[0]?.id,
        limit: 8,
        signal,
      });
    } catch (err) {
      // The item this badge read anchored on was deleted between requests. A
      // disconnect boundary is unknowable now, so retain a conservative fault.
      if (isStaleCursorError(err)) return needsOlderBoundary ? "error" : null;
      const aborted = signal.aborted || (err instanceof Error && err.name === "AbortError");
      if (needsOlderBoundary && !aborted) return "error";
      throw err;
    }
    const combined = latestActivityErrorWindow(
      itemsToBlocks([...older.items, ...page.items]),
      hostOnline,
    );
    if (
      older.hasMore &&
      !combined.boundaryResolved &&
      (combined.state === "disconnected" || combined.state === "recovered_disconnect")
    ) {
      return "error";
    }
    return combined.state ?? null;
  } finally {
    const next = waitingReads.shift();
    if (next) next();
    else activeReads -= 1;
  }
}

function liveError(conversation: ErrorConversation): LatestSessionError | null | undefined {
  const entry = conversationRegistry.peek(conversation.id);
  const state = entry?.getState();
  if (entry?.disposed || !state || state.conversationLoadError !== null) return undefined;
  // The initial history request will supply the latest message. Wait for it
  // even before the stream controller is installed, without a second read.
  if (state.loadingConversation) return null;
  if (state.abortController === null || state.abortController.signal.aborted) {
    return undefined;
  }
  if (state.status === "streaming" || state.terminalPending) return null;
  return latestActivityErrorState(state.blocks, conversation.host_online) ?? null;
}

/** Reuse live transcripts; unopened rows share a cached, bounded tail read. */
export function useSessionErrorStates(
  conversations: readonly ErrorConversation[],
): (LatestSessionError | null)[] {
  const queryClient = useQueryClient();
  const subscribe = useCallback(
    (notify: () => void) => {
      const ids = new Set(conversations.map((c) => c.id));
      const changed = (id: string) => {
        if (!ids.has(id)) return;
        const conversation = conversations.find((candidate) => candidate.id === id);
        if (conversation && liveError(conversation) === undefined) {
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
          const error = liveError(c);
          if (error === undefined) return "?";
          if (error === "error") return "e";
          if (error === "disconnected") return "d";
          if (error === "recovered_disconnect") return "r";
          return "0";
        })
        .join(""),
    [conversations],
  );
  const live = useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
  const queries = useQueries({
    queries: conversations.map((c, i) => ({
      // updated_at is the same freshness signal used by the unread dot and
      // refreshed by the shared session-updates socket/list reconciliation.
      queryKey: ["session-latest-error", c.id, c.updated_at, c.status, c.host_online],
      queryFn: ({ signal }: { signal: AbortSignal }) =>
        fetchLatestError(c.id, c.host_online, signal),
      enabled:
        live[i] === "?" &&
        !isTempConvId(c.id) &&
        !c.provisional &&
        c.status !== "running" &&
        (c.pending_elicitations_count ?? 0) === 0,
      staleTime: Infinity,
      retry: false,
    })),
  });
  return queries.map((query, i) => {
    if (live[i] === "?") return query.data ?? null;
    if (live[i] === "e") return "error";
    if (live[i] === "d") return "disconnected";
    if (live[i] === "r") return "recovered_disconnect";
    return null;
  });
}

/** Boolean compatibility wrapper for consumers that only need fault presence. */
export function useSessionErrors(conversations: readonly ErrorConversation[]): boolean[] {
  return useSessionErrorStates(conversations).map(
    (state) => state === "error" || state === "disconnected",
  );
}
