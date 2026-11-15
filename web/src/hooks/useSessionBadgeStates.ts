import { useCallback, useMemo, useSyncExternalStore } from "react";
import type { Conversation } from "./useConversations";
import { getSessionState, type SessionState } from "./useSessionState";
import {
  latestActivityErrorState,
  latestErrorFromLabels,
  type LatestSessionError,
} from "@/lib/sessionError";
import { conversationRegistry } from "@/store/conversationRegistry";

type BadgeConversation = Pick<
  Conversation,
  | "id"
  | "labels"
  | "host_online"
  | "status"
  | "pending_elicitations_count"
  | "background_work_running"
  | "blocked_on"
>;

/** A healthy loaded transcript's latest error, or undefined when the row's label decides. */
function liveError(conversation: BadgeConversation): LatestSessionError | null | undefined {
  const entry = conversationRegistry.peek(conversation.id);
  const live = entry?.getState();
  if (
    !entry ||
    entry.disposed ||
    !live ||
    live.loadingConversation ||
    live.conversationLoadError !== null ||
    !live.abortController ||
    live.abortController.signal.aborted
  ) {
    return undefined;
  }
  if (live.status === "streaming" || live.terminalPending) return null;
  return (
    latestActivityErrorState(live.blocks, conversation.host_online, live.sessionStatus) ?? null
  );
}

// Lifecycle status always comes from the row, so a stale transcript cannot hide a failure.
function sessionBadgeState(conversation: BadgeConversation): SessionState | null {
  const error = liveError(conversation);
  return getSessionState(
    conversation,
    error === undefined
      ? latestErrorFromLabels(conversation.labels, conversation.host_online)
      : error,
  );
}

/** Reuse existing live transcripts; all other rows need only their list fields. */
export function useSessionBadgeStates(
  conversations: readonly BadgeConversation[],
): (SessionState | null)[] {
  const subscribe = useCallback(
    (notify: () => void) => {
      const ids = new Set(conversations.map((conversation) => conversation.id));
      const changed = (id: string) => {
        if (ids.has(id)) notify();
      };
      const unsubscribe = conversationRegistry.subscribe(changed);
      const unsubscribeDisposed = conversationRegistry.subscribeDisposed(changed);
      return () => {
        unsubscribe();
        unsubscribeDisposed();
      };
    },
    [conversations],
  );
  // Primitive snapshots skip row renders when streamed tokens don't change a badge.
  const getSnapshot = useCallback(
    () => JSON.stringify(conversations.map(sessionBadgeState)),
    [conversations],
  );
  const snapshot = useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
  return useMemo(() => JSON.parse(snapshot) as (SessionState | null)[], [snapshot]);
}
