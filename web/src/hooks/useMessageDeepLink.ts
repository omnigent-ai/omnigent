// Apply ``?message=<id>`` deep links: after the session loads, page in
// older history if needed, then scroll + flash the target message.
// Reuses the same scroll/flash path as the activity rail.

import { useEffect, useRef } from "react";
import { useSearchParams } from "@/lib/routing";
import { MESSAGE_QUERY_PARAM, findMessageElement } from "@/lib/messageDeepLink";
import { scrollToMessage } from "@/hooks/useUserMessageNav";
import { useChatStore } from "@/store/chatStore";

/**
 * When the URL carries ``?message=<id>``, scroll that message into view
 * and highlight it once the session (and enough history) is loaded.
 *
 * @param conversationId - Active session id from the route, or null.
 */
export function useMessageDeepLink(conversationId: string | null): void {
  const [searchParams] = useSearchParams();
  const messageId = searchParams.get(MESSAGE_QUERY_PARAM);
  const loadingConversation = useChatStore((s) => s.loadingConversation);
  const hasMoreHistory = useChatStore((s) => s.hasMoreHistory);
  const loadingMoreHistory = useChatStore((s) => s.loadingMoreHistory);
  const historyGeneration = useChatStore((s) => s.historyGeneration);
  const flashUserMessage = useChatStore((s) => s.flashUserMessage);
  // One successful apply (or give-up) per conversation+message pair.
  const appliedKeyRef = useRef<string | null>(null);

  useEffect(() => {
    if (!conversationId || !messageId) {
      appliedKeyRef.current = null;
      return;
    }
    const key = `${conversationId}:${messageId}`;
    if (appliedKeyRef.current === key) return;
    if (loadingConversation || loadingMoreHistory) return;

    const el = findMessageElement(messageId);
    if (el) {
      appliedKeyRef.current = key;
      scrollToMessage(messageId, flashUserMessage);
      return;
    }

    if (hasMoreHistory) {
      void useChatStore.getState().loadMoreHistory();
      return;
    }

    // Not in the loaded transcript (deleted / wrong id) — stop retrying.
    appliedKeyRef.current = key;
  }, [
    conversationId,
    messageId,
    loadingConversation,
    loadingMoreHistory,
    hasMoreHistory,
    historyGeneration,
    flashUserMessage,
  ]);
}
