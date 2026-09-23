import { useCallback, useSyncExternalStore } from "react";
import type { ConversationState } from "@/store/chatStore";
import { conversationRegistry } from "@/store/conversationRegistry";
import { createInitialConversationState } from "@/store/conversationState";

// A stable empty snapshot for an id that is not (yet) live, so `getSnapshot`
// returns the same reference across renders and `useSyncExternalStore` doesn't
// loop. `getState()` on a live entry is already reference-stable between
// `setState` calls, so the whole hook is stable-by-construction.
const EMPTY_STATE: ConversationState = createInitialConversationState();

/**
 * Reactively read a *specific* conversation's live state from the registry,
 * regardless of which conversation is on screen.
 *
 * The root `useChatStore` only ever projects the ACTIVE entry, so a side chat
 * rendered beside the main chat (its own tab in the Workspace rail) can't read
 * its transcript through `useChatStore`. This subscribes straight to that
 * conversation's registry entry instead — the same per-conversation state the
 * store routes writes to via `entrySetter`/`setterFor`.
 *
 * Returns the whole `ConversationState` (a fresh reference on any change to that
 * conversation), so a panel-scale surface re-renders on each of its own frames;
 * `EMPTY_STATE` is returned for an id with no live entry yet.
 *
 * @param id Conversation id to observe, or null for none.
 */
export function useConversationEntryState(id: string | null): ConversationState {
  const subscribe = useCallback(
    (onChange: () => void) => {
      if (id === null) return () => {};
      return conversationRegistry.subscribe((changedId) => {
        if (changedId === id) onChange();
      });
    },
    [id],
  );
  const getSnapshot = useCallback(() => {
    const entry = id === null ? undefined : conversationRegistry.peek(id);
    return entry === undefined ? EMPTY_STATE : entry.getState();
  }, [id]);
  return useSyncExternalStore(subscribe, getSnapshot);
}
