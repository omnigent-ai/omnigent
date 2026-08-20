import { useEffect, useRef } from "react";
import { useChatStore } from "@/store/chatStore";

/**
 * Refresh session state when a runner becomes reachable.
 *
 * Runner liveness is poll-driven, so the browser can bind while the
 * server still has stale runner-backed fields. A false/unknown -> true
 * edge is the first reliable moment to force a fresh session snapshot.
 */
export function useRefreshSessionStateOnRunnerOnline(
  conversationId: string | null | undefined,
  runnerOnline: boolean | undefined,
): void {
  const previous = useRef<{
    conversationId: string | null | undefined;
    runnerOnline: boolean | undefined;
  }>({ conversationId: undefined, runnerOnline: undefined });

  useEffect(() => {
    const prior = previous.current;
    const changedConversation = prior.conversationId !== conversationId;
    if (
      conversationId &&
      runnerOnline === true &&
      (changedConversation || prior.runnerOnline !== true)
    ) {
      void useChatStore.getState().refreshSessionState(conversationId);
    }
    previous.current = { conversationId, runnerOnline };
  }, [conversationId, runnerOnline]);
}
