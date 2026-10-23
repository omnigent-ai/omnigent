// Apply ``?message=<id>`` deep links and find-bar jumps: after the session
// loads, page in older history if needed, then scroll + flash the target.
// Reuses the same scroll/flash path as the activity rail.

import { useEffect, useMemo, useRef } from "react";
import { useSearchParams } from "@/lib/routing";
import { SESSION_HISTORY_SEEK_PAGE_SIZE } from "@/lib/sessionsApi";
import { FIND_QUERY_PARAM, MESSAGE_QUERY_PARAM, findMessageElement } from "@/lib/messageDeepLink";
import { scrollToMessage } from "@/hooks/useUserMessageNav";
import { useChatStore } from "@/store/chatStore";
import { useTerminalFirst } from "@/shell/TerminalFirstContext";
import { firstFindRange } from "@/components/chat/chatFindReveal";

/** Select Chat from the surface that stays mounted while Terminal is visible. */
export function useMessageDeepLinkChatView(conversationId: string | null): void {
  const [searchParams] = useSearchParams();
  const messageId = searchParams.get(MESSAGE_QUERY_PARAM) ?? searchParams.get(FIND_QUERY_PARAM);
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

/** A message to bring into view; a new `key` starts a new jump. */
export interface MessageJumpTarget {
  key: string;
  /** Ids tried in order, e.g. a matched item's id, then its response id. */
  candidates: string[];
  /** Found text: the jump centers its first occurrence instead of the bubble. */
  query?: string;
}

interface MessageJumpOptions extends MessageDeepLinkOptions {
  /** Map a candidate id to its loaded bubble's anchor id, or null if not loaded. */
  resolveAnchor?: (id: string) => string | null;
}

/** Resolve a message after its transcript and virtualizer are ready. */
export function useMessageDeepLink(
  conversationId: string | null,
  options: MessageDeepLinkOptions = {},
): void {
  const [searchParams] = useSearchParams();
  const messageId = searchParams.get(MESSAGE_QUERY_PARAM);
  const target = useMemo(
    () =>
      conversationId && messageId
        ? { key: `${conversationId}:${messageId}`, candidates: [messageId] }
        : null,
    [conversationId, messageId],
  );
  useMessageJump(target, options);
}

/**
 * Scroll to and flash `target`, paging in older history until one of its
 * candidates resolves to a loaded bubble. Without `resolveAnchor` the first
 * candidate is the anchor id itself.
 */
export function useMessageJump(
  target: MessageJumpTarget | null,
  { ensureMessageVisible, resolveAnchor, ready = true, rangeNonce = 0 }: MessageJumpOptions = {},
): void {
  const terminalFirst = useTerminalFirst();
  const showTerminal = terminalFirst?.isTerminalFirst && terminalFirst.view === "terminal";
  const loadingConversation = useChatStore((s) => s.loadingConversation);
  const hasMoreHistory = useChatStore((s) => s.hasMoreHistory);
  const loadingMoreHistory = useChatStore((s) => s.loadingMoreHistory);
  const historyGeneration = useChatStore((s) => s.historyGeneration);
  const flashUserMessage = useChatStore((s) => s.flashUserMessage);
  const appliedKeyRef = useRef<string | null>(null);

  useEffect(() => {
    if (!target) {
      appliedKeyRef.current = null;
      return;
    }
    const { key, candidates, query } = target;
    if (appliedKeyRef.current === key) return;
    if (!ready || showTerminal || loadingConversation || loadingMoreHistory) return;

    const anchor = resolveAnchor
      ? (candidates.map(resolveAnchor).find((id) => id !== null) ?? null)
      : (candidates[0] ?? null);
    if (anchor !== null) {
      if (findMessageElement(anchor)) {
        appliedKeyRef.current = key;
        // Through the virtualizer first, as the rail does: a bare smooth scroll
        // is cancelled when a row mounting above it corrects scrollTop.
        scrollToMessage(
          anchor,
          flashUserMessage,
          ensureMessageVisible,
          query ? (el) => firstFindRange(el, query) : undefined,
        );
        return;
      }
      // Geometry publishes a new range after this scroll mounts the target.
      if (ensureMessageVisible?.(anchor)) return;
    }
    if (hasMoreHistory) {
      void useChatStore.getState().loadMoreHistory(undefined, SESSION_HISTORY_SEEK_PAGE_SIZE);
      return;
    }
    appliedKeyRef.current = key;
  }, [
    target,
    ready,
    showTerminal,
    loadingConversation,
    loadingMoreHistory,
    hasMoreHistory,
    historyGeneration,
    flashUserMessage,
    ensureMessageVisible,
    resolveAnchor,
    rangeNonce,
  ]);
}
