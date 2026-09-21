// Apply ``?message=<id>`` deep links: after the session loads, page in
// older history if needed, then scroll + flash the target message.
// Reuses the same scroll/flash path as the activity rail.

import { useEffect, useRef } from "react";
import { useSearchParams } from "@/lib/routing";
import { MESSAGE_QUERY_PARAM, findMessageElement } from "@/lib/messageDeepLink";
import { scrollToMessage } from "@/hooks/useUserMessageNav";
import { useChatStore } from "@/store/chatStore";
import { useTerminalFirst } from "@/shell/TerminalFirstContext";

/** Select Chat from the surface that stays mounted while Terminal is visible. */
export function useMessageDeepLinkChatView(conversationId: string | null): void {
  const [searchParams] = useSearchParams();
  const messageId = searchParams.get(MESSAGE_QUERY_PARAM);
  const terminalFirst = useTerminalFirst();
  const showTerminal = terminalFirst?.isTerminalFirst && terminalFirst.view === "terminal";
  const setView = terminalFirst?.setView;
  const loadingConversation = useChatStore((s) => s.loadingConversation);
  const appliedKeyRef = useRef<string | null>(null);
  useEffect(() => {
    if (!conversationId || !messageId) {
      appliedKeyRef.current = null;
      return;
    }
    if (loadingConversation) return;
    const key = `${conversationId}:${messageId}`;
    if (appliedKeyRef.current === key) return;
    appliedKeyRef.current = key;
    if (showTerminal) setView?.("chat");
  }, [conversationId, messageId, loadingConversation, showTerminal, setView]);
}

interface MessageDeepLinkOptions {
  /** Mount a loaded message that is outside the virtualized window. */
  ensureMessageVisible?: (messageId: string) => boolean;
  ready?: boolean;
  rangeNonce?: number;
}

/** Resolve a message after its transcript and virtualizer are ready. */
export function useMessageDeepLink(
  conversationId: string | null,
  { ensureMessageVisible, ready = true, rangeNonce = 0 }: MessageDeepLinkOptions = {},
): void {
  const [searchParams] = useSearchParams();
  const messageId = searchParams.get(MESSAGE_QUERY_PARAM);
  const terminalFirst = useTerminalFirst();
  const showTerminal = terminalFirst?.isTerminalFirst && terminalFirst.view === "terminal";
  const loadingConversation = useChatStore((s) => s.loadingConversation);
  const hasMoreHistory = useChatStore((s) => s.hasMoreHistory);
  const loadingMoreHistory = useChatStore((s) => s.loadingMoreHistory);
  const historyGeneration = useChatStore((s) => s.historyGeneration);
  const flashUserMessage = useChatStore((s) => s.flashUserMessage);
  const appliedKeyRef = useRef<string | null>(null);

  useEffect(() => {
    if (!conversationId || !messageId) {
      appliedKeyRef.current = null;
      return;
    }
    const key = `${conversationId}:${messageId}`;
    if (appliedKeyRef.current === key) return;
    if (!ready || showTerminal || loadingConversation || loadingMoreHistory) return;

    if (findMessageElement(messageId)) {
      appliedKeyRef.current = key;
      scrollToMessage(messageId, flashUserMessage);
      return;
    }
    // Geometry publishes a new range after this scroll mounts the target.
    if (ensureMessageVisible?.(messageId)) return;
    if (hasMoreHistory) {
      void useChatStore.getState().loadMoreHistory();
      return;
    }
    appliedKeyRef.current = key;
  }, [
    conversationId,
    messageId,
    ready,
    showTerminal,
    loadingConversation,
    loadingMoreHistory,
    hasMoreHistory,
    historyGeneration,
    flashUserMessage,
    ensureMessageVisible,
    rangeNonce,
  ]);
}
