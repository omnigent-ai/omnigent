// The status a session's sidebar row shows: waiting for the viewer, running, starting up, an
// error or a lost host, or new messages. Anywhere else that names the session can show the same.
import { useMemo } from "react";

import type { Conversation } from "@/hooks/useConversations";
import { useSessionBadgeStates } from "@/hooks/useSessionBadgeStates";
import type { SessionState } from "@/hooks/useSessionState";
import { useConversationReadState } from "@/hooks/useUnseenConversations";
import { isConversationMuted, showsUnreadFor } from "@/lib/sessionMute";
import { useChatStore } from "@/store/chatStore";

export interface SessionIndicatorState {
  /** What the session's badge shows, or null for nothing. */
  sessionState: SessionState | null;
  /** Whether the session counts as unread: seen state, not muting. */
  unread: boolean;
  /** Whether the new-messages dot shows: unread and not muted. */
  hasUnseenMessages: boolean;
  isMuted: boolean;
}

/**
 * The session's status for its badge.
 *
 * @param conversation - The session's list row.
 * @param isActive - Whether the viewer is reading the session now, so its new messages are
 *   already seen unless the viewer marked it unread.
 */
export function useSessionIndicatorState(
  conversation: Conversation,
  isActive: boolean,
): SessionIndicatorState {
  // A write for another conversation leaves this primitive snapshot unchanged,
  // so useSyncExternalStore skips the heavy row render.
  const readState = useConversationReadState(
    conversation.id,
    conversation.updated_at,
    conversation.status,
  );
  // Explicit unread overrides active-row suppression; running turns remain
  // hidden through readState.unseen. Muting hides the dot without marking read.
  const isMuted = isConversationMuted(conversation);
  const unread = readState.unseen && (!isActive || readState.explicitlyUnread);
  const hasUnseenMessages = showsUnreadFor(conversation, unread);
  // Approvals and failures outrank the unread dot without clearing read state.
  const badgeConversations = useMemo(() => [conversation], [conversation]);
  const [derivedState] = useSessionBadgeStates(badgeConversations);
  // The bound session's launch/relaunch window: a send is in flight (local
  // status "streaming") or the runner is auto-creating the PTY
  // (`terminalPending`), but the server hasn't confirmed `running` yet — a
  // cold boot, or a send waking a disconnected runner. Without this the badge
  // shows nothing while the session is visibly "Starting up…" in the chat.
  // Only the bound (open) conversation has this store state; others read
  // false, and the server-derived states above win once they land.
  const isStartingUp = useChatStore(
    (s) => s.conversationId === conversation.id && (s.status === "streaming" || s.terminalPending),
  );
  const sessionState: SessionState | null =
    derivedState?.kind === "awaiting" || derivedState?.kind === "running"
      ? derivedState
      : isStartingUp
        ? { kind: "starting" }
        : (derivedState ?? (hasUnseenMessages ? { kind: "unseen" } : null));
  return { sessionState, unread, hasUnseenMessages, isMuted };
}
