import { useEffect } from "react";

import { reportConversationShown } from "@/lib/nativeBridge";

/** What the chat page knows about the conversation in its address. */
export interface ConversationOnScreen {
  /** The route's server session id; undefined off a conversation. */
  conversationId: string | undefined;
  /** The id of the session whose details have loaded, if any. */
  loadedSessionId: string | undefined;
  /** The conversation whose messages the page holds. */
  activeConversationId: string | null;
  /** True while those messages are still loading. */
  loading: boolean;
  /** Set when the conversation failed to load. */
  loadError: unknown;
}

/**
 * The conversation the page is really showing: its details and its messages
 * have both loaded, for a signed-in user who may read it. Null while it is
 * loading, failed, or a different conversation is still on screen.
 */
export function conversationOnScreen({
  conversationId,
  loadedSessionId,
  activeConversationId,
  loading,
  loadError,
}: ConversationOnScreen): string | null {
  if (!conversationId || loading || loadError) return null;
  if (loadedSessionId !== conversationId || activeConversationId !== conversationId) return null;
  return conversationId;
}

/**
 * Tell the desktop shell which conversation this page has on screen, so it can
 * tell a browser the conversation really opened.
 */
export function useReportConversationShown(state: ConversationOnScreen): void {
  const shown = conversationOnScreen(state);
  useEffect(() => {
    reportConversationShown(shown);
    return () => reportConversationShown(null);
  }, [shown]);
}
