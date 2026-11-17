// Starting and closing side chats from the workspace rail. A side chat opens as an
// empty draft tab; its first message creates the child conversation.

import { toast } from "sonner";

import { createSideChat, stopSession } from "@/lib/sessionsApi";
import { SIDE_CHAT_COMMAND_PREFIX, usesNativeSideChatFork } from "@/lib/sideChat";
import { subagentSessionIdFromTab } from "@/lib/subagentTabs";
import { useChatStore } from "@/store/chatStore";

export interface StartSideChatOptions {
  conversationId: string;
  harness: Parameters<typeof usesNativeSideChatFork>[0];
  parentAgentId: string | null;
  /** The draft tab's id (a `pending:` id), which also keys its composer. */
  draftKey: string;
  text: string;
  /**
   * Draft tabs waiting for a natively forked child, oldest first. Codex's child
   * arrives later with no id to pair on, so it fills the oldest waiting draft.
   */
  awaitingDrafts: { current: string[] };
  /** The child exists: point the draft's tab at it. */
  onCreated: (childId: string) => void;
}

/**
 * Send a draft side chat's first message, creating its child conversation. Codex
 * forks in-process from a `/side` message on the parent; every other harness forks
 * server-side. Rejects so the composer re-enables and keeps the text for a retry.
 */
export function startSideChat({
  conversationId,
  harness,
  parentAgentId,
  draftKey,
  text,
  awaitingDrafts,
  onCreated,
}: StartSideChatOptions): Promise<void> {
  if (usesNativeSideChatFork(harness)) {
    if (parentAgentId === null) return Promise.reject(new Error("no agent"));
    // `send` resolves after its internal catch, so a failure (e.g. the host is too
    // old to fork) has set `sendFailed` by the time the await returns; reject then
    // and drop the queued draft so it never waits for a child that isn't coming.
    awaitingDrafts.current.push(draftKey);
    let sendFailed = false;
    return useChatStore
      .getState()
      .send(SIDE_CHAT_COMMAND_PREFIX + text, parentAgentId, undefined, {
        pinnedConversationId: conversationId,
        onError: (message) => {
          sendFailed = true;
          awaitingDrafts.current = awaitingDrafts.current.filter((id) => id !== draftKey);
          toast.error(message);
        },
      })
      .then(() => {
        if (sendFailed) throw new Error("side chat send failed");
      });
  }
  return createSideChat(
    conversationId,
    useChatStore.getState().sideChatComposers[draftKey]?.settings,
  ).then(
    ({ childSessionId }) => {
      // We have the child id, so point THIS draft at it directly: overlapping
      // launches can't cross-assign. Seeding the draft fires sideChatToOpen,
      // which then just re-selects and reveals.
      onCreated(childSessionId);
      // The text moved to the real child; a failed create keeps the draft for a retry.
      useChatStore.getState().clearSideChatComposer(draftKey);
      useChatStore.getState().openSideChatWithDraft(childSessionId, text, conversationId);
    },
    (err) => {
      toast.error("Couldn't start a side chat for this session.");
      throw err;
    },
  );
}

/**
 * Stop a side chat, then call `onStopped` so its tab can close; a failed stop toasts
 * and keeps the tab. Sub-agent and draft tabs close in the same tick: a sub-agent
 * tab only hides, and a draft has nothing to stop.
 */
export async function stopSideChat(childId: string, onStopped: () => void): Promise<void> {
  if (subagentSessionIdFromTab(childId) !== null) {
    onStopped();
    return;
  }
  // Take the queue first so the stop's idle status can't send it to the closing chat.
  const queued = useChatStore.getState().clearQueuedMessages(childId);
  if (!childId.startsWith("pending:")) {
    try {
      await stopSession(childId);
    } catch {
      useChatStore.getState().restoreQueuedMessages(queued);
      toast.error("Couldn't close side chat. Try again.");
      return;
    }
  }
  // The tab is gone, so its unsent text, follow-ups queued during the stop, and any
  // seeded question that never got to send have nowhere to return to.
  useChatStore.getState().clearQueuedMessages(childId);
  useChatStore.getState().clearSideChatComposer(childId);
  useChatStore.getState().clearSideChatDraft(childId);
  onStopped();
}
