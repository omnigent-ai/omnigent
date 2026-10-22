import { createContext, useContext } from "react";
import { useChatStore } from "@/store/chatStore";

/**
 * The conversation a bubble subtree belongs to. `null` (the default) means the
 * active/root conversation the root store projects — i.e. the main chat.
 *
 * A side-chat pane renders the SAME bubble components beside the main chat, but
 * those components reach back into the root store for interactive actions
 * (approve, retry, attachment URLs), which would target the main conversation.
 * `SideChatPane` overrides this with the child id so those actions target the
 * child instead.
 */
export const ConversationScopeContext = createContext<string | null>(null);

/**
 * The conversation id a bubble action should target: the scoped child when
 * inside a `ConversationScopeContext.Provider`, else the root store's active
 * conversation. Both hooks always run (no conditional-hook hazard); the root
 * subscription is simply unused when a scope is present.
 */
export function useScopedConversationId(): string | null {
  const scoped = useContext(ConversationScopeContext);
  const active = useChatStore((s) => s.conversationId);
  return scoped ?? active;
}
